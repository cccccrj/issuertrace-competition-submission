"""Access control for the platform.

The platform serves a collected corpus and its adjudications, so an open port is
not an acceptable default.  Authentication is therefore always on.

账号本身住在库里（``api/users_db``），不再是手写的配置文件。此前那套有两个具体
后果：忘记口令只能去改 JSON 重算哈希，而且没有账号文件时会退化成「启动生成一个
一次性口令」——一个人一把口令，谈不上用户管理。**那条兜底已经去掉**：没有任何
账号时，用 ``python -m api.users create`` 建第一个管理员，而不是让服务自己开门。

Roles exist because review is a workflow with two sides: an analyst proposes a
judgement, a reviewer accepts or rejects it, and a viewer may read neither's
work into the record.  ``admin`` 在它们之上单开一级，只管账号：能审批业务与能发
账号是两件事，绑在一起等于让每个复核负责人都能给自己加权限。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

COOKIE_NAME = "lcei_session"
# Ordered by authority: each role can do everything the ones before it can.
ROLES = ("viewer", "analyst", "reviewer", "admin")
# 只在首次启动时被 users_db 读一次，用来把既有账号搬进表里；之后不再是账号来源。
ACCOUNTS_PATH = Path(__file__).resolve().parent.parent / "config" / "accounts.json"
PBKDF2_ITERATIONS = 240_000
SESSION_TTL_SECONDS = 12 * 3600
# A demo box is not a login endpoint worth brute-forcing quickly, but throttling
# is cheap and keeps a mistyped script from turning into a password search.
MAX_ATTEMPTS = 10
ATTEMPT_WINDOW_SECONDS = 300

# Paths that must answer before anyone can be logged in: the sign-in endpoints
# themselves, and a liveness check that reveals no corpus data.
PUBLIC_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/auth/session", "/api/health"}

_generated_secret: str | None = None
_attempts: dict[str, list[float]] = {}


def signing_secret() -> bytes:
    """The key that signs sessions.

    Generated per process when unset, which means a restart invalidates every
    session.  That is the safer default: a leaked cookie from an old demo run
    must not survive into the next one.
    """
    global _generated_secret
    configured = os.environ.get("AUTH_SECRET")
    if configured:
        return configured.encode()
    if _generated_secret is None:
        _generated_secret = secrets.token_hex(32)
    return _generated_secret.encode()


def hash_password(password: str, *, salt: str | None = None) -> str:
    """A stored password, in a form that a leaked accounts file does not reveal."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


# A stored password of the same shape and cost as a real one, so an unknown
# username can be checked against something instead of returning early.  It is
# built once at import: deriving it per attempt made the unknown-username path
# cost a hash *plus* a verify, which is more work than the single verify it was
# meant to imitate — the timing then answered "does this account exist" in the
# opposite direction.
_DECOY_HASH = hash_password(secrets.token_hex(16))


def accounts() -> dict[str, dict[str, Any]]:
    """The accounts that exist, keyed by username.

    Read on every call rather than cached: adding a reviewer during a demo
    should not require restarting the platform and invalidating every session.
    停用的账号不在这里——它们不能登录，但名字仍留在审计记录里。
    """
    from api import users_db

    db = users_db.connect()
    try:
        rows = db.execute("SELECT username, role, password_hash FROM platform_users "
                          "WHERE active=1").fetchall()
    finally:
        db.close()
    return {row["username"]: {"password_hash": row["password_hash"], "role": row["role"]}
            for row in rows if row["role"] in ROLES}


def startup_notice() -> str | None:
    """没有任何账号时该说的话。

    此前这里会生成一个一次性口令并打印——那让「没有用户体系」看起来像有。现在
    它只指路：建第一个管理员是一条命令的事，不该由服务替谁开门。
    """
    if accounts():
        return None
    return ("[auth] 库里还没有任何账号。建第一个管理员：\n"
            "[auth]   PYTHONPATH=. .venv/bin/python -m api.users create "
            "--username admin --role admin")


def sign(payload: str) -> str:
    digest = hmac.new(signing_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_token(username: str, role: str = "reviewer", now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + SESSION_TTL_SECONDS)
    # JSON rather than a delimited string: a username containing the delimiter
    # would otherwise parse into a different account.
    payload = json.dumps({"u": username, "r": role, "e": expires},
                         separators=(",", ":"), sort_keys=True)
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{sign(payload)}"


def read_token(token: str | None, now: float | None = None) -> dict[str, str] | None:
    """The account a token proves, or None if it proves nothing."""
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        claims = json.loads(payload)
        expiry = int(claims["e"])
        username, role = str(claims["u"]), str(claims["r"])
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return None
    # Constant-time comparison: a signature check that leaks its progress by
    # timing is not a signature check.
    if not hmac.compare_digest(signature, sign(payload)):
        return None
    if (now if now is not None else time.time()) >= expiry:
        return None
    # A role that no longer exists is refused rather than honoured: the token is
    # authentic, but the authority it names is not.
    return {"username": username, "role": role} if role in ROLES else None


def throttled(client: str, now: float | None = None) -> bool:
    moment = now if now is not None else time.monotonic()
    recent = [stamp for stamp in _attempts.get(client, []) if moment - stamp < ATTEMPT_WINDOW_SECONDS]
    _attempts[client] = recent
    return len(recent) >= MAX_ATTEMPTS


def record_failure(client: str, now: float | None = None) -> None:
    _attempts.setdefault(client, []).append(now if now is not None else time.monotonic())


def clear_failures(client: str) -> None:
    _attempts.pop(client, None)


def check(username: str, password: str) -> str | None:
    """The role this pair of credentials proves, or None."""
    entry = accounts().get(username or "")
    # Exactly one verification on every path — known or unknown, right or
    # wrong — so response time does not answer "does this account exist".
    stored = entry["password_hash"] if entry else _DECOY_HASH
    matched = verify_password(password or "", stored)
    if not (entry and matched):
        return None
    from api import users_db

    db = users_db.connect()
    try:
        users_db.note_login(db, username)
    finally:
        db.close()
    return entry["role"]


def allows(role: str | None, required: str) -> bool:
    """Whether ``role`` carries at least the authority of ``required``."""
    if role not in ROLES or required not in ROLES:
        return False
    return ROLES.index(role) >= ROLES.index(required)


def main() -> None:
    """Generate a password hash.  账号管理请用 `python -m api.users`。"""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("password", help="the password to hash; it is never stored in plain text")
    print(hash_password(parser.parse_args().password))


def live_session(token: str | None, now: float | None = None) -> dict[str, str] | None:
    """签名有效**且账号此刻仍然有效**的会话。

    只验签名不够：一个被停用的账号，或者被降权的账号，在令牌到期前都还能拿着旧
    Cookie 继续用——12 小时里「已停用」等于没停用。角色也以库里此刻的值为准，
    所以改角色立刻生效，不必等对方重新登录。
    """
    session = read_token(token, now)
    if not session:
        return None
    from api import users_db

    account = users_db.lookup(session["username"])
    if not account:
        return None
    return {"username": account["username"], "role": account["role"]}


def session_state(token: str | None) -> dict[str, Any]:
    session = live_session(token)
    return {"authenticated": bool(session),
            "username": session["username"] if session else None,
            "role": session["role"] if session else None}


if __name__ == "__main__":
    main()
