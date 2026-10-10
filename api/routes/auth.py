"""Sign-in endpoints.

The three routes here are the only ones that answer without a session, so they
are deliberately dull: they read no business table and return no corpus data.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from api import auth, platform_audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


def current_session(request: Request) -> dict[str, str]:
    """The signed-in account, for routes that need to know who is acting.

    The middleware has already refused anonymous callers, so reaching a route
    with no session means the gate was bypassed; failing closed here keeps that
    from turning into an anonymous write.
    """
    session = auth.live_session(request.cookies.get(auth.COOKIE_NAME))
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return session


def requires(role: str):
    """Dependency factory: refuse anyone below ``role``."""
    def dependency(session: dict[str, str] = Depends(current_session)) -> dict[str, str]:
        if not auth.allows(session.get("role"), role):
            raise HTTPException(status_code=403, detail=f"该操作需要 {role} 权限")
        return session
    return dependency


class Credentials(BaseModel):
    username: str
    password: str


def client_of(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/session")
def session(request: Request) -> dict[str, Any]:
    """Who, if anyone, the current cookie proves this caller to be."""
    return auth.session_state(request.cookies.get(auth.COOKIE_NAME))


@router.post("/login")
def login(credentials: Credentials, request: Request, response: Response) -> dict[str, Any]:
    client = client_of(request)
    # 三条出口各留一行。**留的是账号与来源，不是口令**——一份记下了口令的审计日志，
    # 本身就是那个口令的第二个存放处；而「谁在试」「从哪里试」「试了多少次」才是
    # 事后要查的东西。限流那一条尤其不能省：它正是「有人在撞口令」的那个信号。
    if auth.throttled(client):
        platform_audit.record("login", actor=credentials.username, outcome="refused",
                              client=client, detail="尝试次数过多，已限流")
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
    role = auth.check(credentials.username, credentials.password)
    if not role:
        auth.record_failure(client)
        platform_audit.record("login", actor=credentials.username, outcome="refused",
                              client=client, detail="账号或密码不正确")
        # One message for both wrong-user and wrong-password: telling a caller
        # which half was right is telling them half the answer.
        raise HTTPException(status_code=401, detail="账号或密码不正确")
    auth.clear_failures(client)
    platform_audit.record("login", actor=credentials.username, role=role, client=client)
    response.set_cookie(
        auth.COOKIE_NAME, auth.issue_token(credentials.username, role),
        max_age=auth.SESSION_TTL_SECONDS, httponly=True, samesite="lax",
        # The cookie is only marked Secure over HTTPS; forcing it on a local
        # http demo would make the session silently impossible to set.
        secure=request.url.scheme == "https",
    )
    return {"authenticated": True, "username": credentials.username, "role": role,
            "expires_in": auth.SESSION_TTL_SECONDS}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    # 登出没有会话也照样成立（Cookie 过期了再点一次退出），所以这里不拦，
    # 只在认得出人的时候才记一行——记一行 actor 是「未知」的登出没有任何用处。
    session = auth.live_session(request.cookies.get(auth.COOKIE_NAME))
    if session:
        platform_audit.record("logout", actor=session["username"], role=session.get("role"),
                              client=client_of(request))
    response.delete_cookie(auth.COOKIE_NAME, httponly=True, samesite="lax")
    return {"authenticated": False}
