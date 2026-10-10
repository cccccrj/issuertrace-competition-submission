"""账号的存放处：从手写的配置文件搬到库里，并且可以在平台上管理。

在这之前，账号是 ``config/accounts.json`` 里的三行 JSON 加 pbkdf2 哈希，除此之外
还有一条「没有这个文件时，启动生成一次性口令」的兜底。两者合起来的后果很具体：
**忘记密码只能去手改配置文件重算哈希**，加一个人也一样；平台自己没有任何入口。
这个模块把账号变成一张表，于是增删、改角色、改自己的密码、停用，都能在页面上做，
并且每一次改动都留下是谁、什么时候。

写入走 ``api/review_db`` 那条既有的路——分析库由 ``api/db`` 只读打开，全平台只有
人工产生的事实可以写，账号正是这一类。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from api.review_db import connect as write_connect, now as utc_now

# 四级，逐级包含。admin 从 reviewer 上面单开一级：能审批业务与能发账号是两件事，
# 把它们绑在一起，等于让每个复核负责人都能给自己加权限。
ROLES = ("viewer", "analyst", "reviewer", "admin")
ROLE_LABELS = {"viewer": "只读", "analyst": "分析师", "reviewer": "核查负责人", "admin": "管理员"}

MIN_PASSWORD_LENGTH = 8


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS platform_users(
        username TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        created_by TEXT,
        password_changed_at TEXT NOT NULL,
        last_login_at TEXT)""")
    db.commit()


def connect() -> sqlite3.Connection:
    db = write_connect()
    ensure_schema(db)
    adopt_legacy_accounts(db)
    return db


def adopt_legacy_accounts(db: sqlite3.Connection) -> int:
    """把 config/accounts.json 里的账号搬进表里，一次性，且只在表为空时。

    连口令一起搬——存的本来就是 pbkdf2 哈希，原样挪过去，现有账号照常能登录。
    搬完之后那个文件不再被读取；留着它是为了让部署过的机器有个回头路，不是为了
    继续当账号来源。
    """
    if db.execute("SELECT count(*) FROM platform_users").fetchone()[0]:
        return 0
    # 路径只有一处定义（api.auth.ACCOUNTS_PATH）。曾经两个模块各写一份，测试改了
    # 其中一个，正式部署的账号就被导进了临时库。
    from api.auth import ACCOUNTS_PATH

    if not ACCOUNTS_PATH.is_file():
        return 0
    try:
        payload = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    moved = 0
    for entry in payload.get("accounts", []):
        username, stored = entry.get("username"), entry.get("password_hash")
        role = entry.get("role", "viewer")
        if not (username and stored and role in ROLES):
            continue
        db.execute("""INSERT OR IGNORE INTO platform_users
            (username, role, password_hash, active, created_at, created_by, password_changed_at)
            VALUES(?,?,?,1,?,?,?)""",
            (str(username), role, str(stored), utc_now(), "accounts.json 导入", utc_now()))
        moved += 1
    db.commit()
    return moved


def lookup(username: str) -> dict[str, Any] | None:
    """一次只读的账号查询，给每个请求的会话校验用。

    不走 ``connect()``：那条路每次都要建表并检查一次历史账号导入，对一个每请求
    都要跑的检查来说太贵。读的是写入路径那同一个库文件——「用户表在哪」只能有
    一个答案，否则会出现「写进了一个库、查的是另一个」。表还不存在时当作没有账号。
    """
    from api.review_db import DB_PATH

    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    try:
        exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name='platform_users'").fetchone()
        if not exists:
            return None
        row = db.execute("SELECT username, role, active FROM platform_users WHERE username=?",
                         (username or "",)).fetchone()
    finally:
        db.close()
    if not row or not row["active"]:
        return None
    return {"username": row["username"], "role": row["role"]}


def row_of(db: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM platform_users WHERE username=?", (username or "",)).fetchone()


def listing(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """所有账号。不含口令哈希——它没有任何理由离开这个模块。"""
    rows = db.execute("""SELECT username, role, active, created_at, created_by,
                                password_changed_at, last_login_at
                         FROM platform_users ORDER BY username""").fetchall()
    return [{**dict(row), "active": bool(row["active"]),
             "role_label": ROLE_LABELS.get(row["role"], row["role"])} for row in rows]


def password_problem(username: str, password: str) -> str | None:
    """口令不合格的理由，合格则为 None。

    只有两条，都是能被机器判定的：长度，以及不能就是用户名。更复杂的规则（大小写
    混合、特殊字符）在这里挡不住真正的弱口令，只会让人把口令写在便签上。
    """
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"密码至少 {MIN_PASSWORD_LENGTH} 位"
    if (password or "").strip().lower() == (username or "").strip().lower():
        return "密码不能与账号相同"
    return None


def create(db: sqlite3.Connection, username: str, password: str, role: str,
           *, by: str, hasher) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"未知角色：{role}")
    username = (username or "").strip()
    if not username:
        raise ValueError("账号不能为空")
    if row_of(db, username):
        raise ValueError(f"账号已存在：{username}")
    problem = password_problem(username, password)
    if problem:
        raise ValueError(problem)
    now = utc_now()
    db.execute("""INSERT INTO platform_users
        (username, role, password_hash, active, created_at, created_by, password_changed_at)
        VALUES(?,?,?,1,?,?,?)""", (username, role, hasher(password), now, by, now))
    db.commit()
    return {"username": username, "role": role}


def set_password(db: sqlite3.Connection, username: str, password: str, *, hasher) -> None:
    problem = password_problem(username, password)
    if problem:
        raise ValueError(problem)
    if not row_of(db, username):
        raise ValueError(f"没有这个账号：{username}")
    db.execute("UPDATE platform_users SET password_hash=?, password_changed_at=? WHERE username=?",
               (hasher(password), utc_now(), username))
    db.commit()


def set_role(db: sqlite3.Connection, username: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"未知角色：{role}")
    if not row_of(db, username):
        raise ValueError(f"没有这个账号：{username}")
    db.execute("UPDATE platform_users SET role=? WHERE username=?", (role, username))
    db.commit()


def set_active(db: sqlite3.Connection, username: str, active: bool) -> None:
    """停用而不是删除。

    审计日志、复核判定、处置记录里都留着这个人的名字；把账号行删掉，那些记录就
    指向一个不存在的人。停用让「他不能再登录」和「他做过什么」这两件事分开。
    """
    if not row_of(db, username):
        raise ValueError(f"没有这个账号：{username}")
    db.execute("UPDATE platform_users SET active=? WHERE username=?", (int(active), username))
    db.commit()


def note_login(db: sqlite3.Connection, username: str) -> None:
    db.execute("UPDATE platform_users SET last_login_at=? WHERE username=?", (utc_now(), username))
    db.commit()


def admins(db: sqlite3.Connection) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT username FROM platform_users WHERE role='admin' AND active=1")]
