"""账号管理。

平台此前没有这一层：账号是手写的 JSON，忘记口令只能去改文件重算哈希。这里把增删、
改角色、改口令、停用都放到接口上，并且分成两类权限——**改自己的口令谁都可以，
其余一律要 admin**。

停用而不是删除：审计日志、复核判定与处置记录里都留着这个人的名字，删掉账号行会让
那些记录指向一个不存在的人。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

from api import platform_audit, users_db
from api.auth import ROLES, allows, check, hash_password
from api.routes.auth import client_of, current_session, requires

router = APIRouter(prefix="/api/users", tags=["users"])


def db_session():
    db = users_db.connect()
    try:
        yield db
    finally:
        db.close()


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class RoleChange(BaseModel):
    role: str


class PasswordReset(BaseModel):
    password: str


class OwnPassword(BaseModel):
    current: str
    password: str


def refuse(exception: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exception))


def trace(action: str, username: str, session: dict[str, Any], request: Request, *,
          outcome: str = "ok", detail: str | None = None) -> None:
    """一次权限变更留一行：谁改的、改了谁、改成什么、从哪台机器。

    账号表本身只存**当前值**——某人现在是核查负责人。它答不出「谁把他改上去的、
    什么时候」，而这正是权限这件事要被问到的东西。

    **被拒绝的也记。** 「有人试图把最后一个管理员降级」与「有人把某人升成了管理员」
    是同一类事实的两面；只记成功的那一半，剩下的一半就只存在于当时那个人的屏幕上。
    """
    platform_audit.record(action, actor=session["username"], role=session.get("role"),
                          object_kind="account", object_id=username, outcome=outcome,
                          client=client_of(request), detail=detail)


@router.get("")
def list_users(session=Depends(requires("admin")), db=Depends(db_session)) -> dict[str, Any]:
    return {"roles": [{"value": role, "label": users_db.ROLE_LABELS[role]} for role in ROLES],
            "items": users_db.listing(db)}


@router.get("/reviewers")
def reviewers(session=Depends(requires("analyst")), db=Depends(db_session)) -> dict[str, Any]:
    """能接上报的人：此刻有效、且有复核权的账号，只给名字和角色。

    账号列表本身只对管理员开放；分析师上报时要选「交给谁」，需要的只是这一小截——
    不带创建时间、最近登录这些与上报无关的东西。
    """
    return {"items": [{"username": row["username"], "role": row["role"],
                       "role_label": row["role_label"]}
                      for row in users_db.listing(db)
                      if row["active"] and allows(row["role"], "reviewer")]}


@router.post("")
def create_user(body: NewUser, request: Request, session=Depends(requires("admin")),
                db=Depends(db_session)) -> dict[str, Any]:
    try:
        created = users_db.create(db, body.username, body.password, body.role,
                                  by=session["username"], hasher=hash_password)
    except ValueError as exception:
        raise refuse(exception)
    trace("account_create", body.username, session, request,
          detail=f"角色 {users_db.ROLE_LABELS.get(body.role, body.role)}")
    return created


# `/me/...` 必须声明在 `/{username}/...` 之前：FastAPI 按声明顺序匹配，
# 否则 "me" 会被当成用户名，落到需要 admin 的那条路由上。
@router.post("/me/password")
def change_own_password(body: OwnPassword, request: Request, session=Depends(current_session),
                        db=Depends(db_session)) -> dict[str, Any]:
    """改自己的口令。任何角色都可以，但要先验旧口令。

    验旧口令挡的是「有人借着一台没锁屏的机器把口令改掉」——会话本身证明不了
    坐在这里的还是同一个人。
    """
    if not check(session["username"], body.current):
        # 口令不进 detail，连长度都不记——记下来的每一个字都是它的第二个存放处。
        trace("password_change", session["username"], session, request,
              outcome="refused", detail="当前密码不对")
        raise HTTPException(status_code=403, detail="当前密码不对")
    try:
        users_db.set_password(db, session["username"], body.password, hasher=hash_password)
    except ValueError as exception:
        raise refuse(exception)
    trace("password_change", session["username"], session, request)
    return {"username": session["username"], "changed": True}


@router.post("/{username}/role")
def change_role(username: str, body: RoleChange, request: Request,
                session=Depends(requires("admin")), db=Depends(db_session)) -> dict[str, Any]:
    # 最后一个管理员不能把自己降下去：那会让这台机器再也没有人能发账号，
    # 只能回到命令行——正是这次改动要消掉的那种死角。
    if (username == session["username"] and body.role != "admin"
            and users_db.admins(db) == [username]):
        trace("role_change", username, session, request,
              outcome="refused", detail="最后一个管理员不能降自己")
        raise HTTPException(status_code=400, detail="你是最后一个管理员，先给别人授权再降自己")
    # 旧角色要在改之前读，否则留下的是「改成了核查负责人」而不是「从分析师改成了
    # 核查负责人」——后者才回答得了「这个人的权限是什么时候长出来的」。
    before = users_db.row_of(db, username)
    try:
        users_db.set_role(db, username, body.role)
    except ValueError as exception:
        raise refuse(exception)
    label = users_db.ROLE_LABELS.get
    trace("role_change", username, session, request,
          detail=f"{label(before['role'], before['role']) if before else '—'}"
                 f" → {label(body.role, body.role)}")
    return {"username": username, "role": body.role}


@router.post("/{username}/password")
def reset_password(username: str, body: PasswordReset, request: Request,
                   session=Depends(requires("admin")), db=Depends(db_session)) -> dict[str, Any]:
    try:
        users_db.set_password(db, username, body.password, hasher=hash_password)
    except ValueError as exception:
        raise refuse(exception)
    trace("password_reset", username, session, request)
    return {"username": username, "reset_by": session["username"]}


@router.post("/{username}/active")
def set_active(username: str, request: Request, active: bool = Body(embed=True),
               session=Depends(requires("admin")), db=Depends(db_session)) -> dict[str, Any]:
    if username == session["username"] and not active:
        trace("active_change", username, session, request, outcome="refused", detail="不能停用自己")
        raise HTTPException(status_code=400, detail="不能停用自己")
    if not active and users_db.admins(db) == [username]:
        trace("active_change", username, session, request,
              outcome="refused", detail="最后一个管理员不能停用")
        raise HTTPException(status_code=400, detail="这是最后一个管理员，停用后没人能发账号")
    try:
        users_db.set_active(db, username, active)
    except ValueError as exception:
        raise refuse(exception)
    trace("active_change", username, session, request, detail="启用" if active else "停用")
    return {"username": username, "active": active}
