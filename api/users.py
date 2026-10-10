"""账号管理的命令行入口——第一个管理员由它建，之后都在页面上做。

之所以留一条命令行：库里一个账号都没有的时候，页面上没有人能登录去建账号。
此前这个位置是「启动生成一次性口令」，那让「没有用户体系」看起来像有一个。
一条命令是更诚实的形态——它要求有人登上这台机器，而不是让服务替谁开门。

口令一律交互式输入（`getpass`），不接受命令行参数：写在参数里的口令会进
shell 历史、进 `ps` 的输出、进运维脚本的日志。
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from api import users_db
from api.auth import hash_password


def ask_password(username: str) -> str:
    first = getpass.getpass("口令（不回显）：")
    again = getpass.getpass("再输一次：")
    if first != again:
        raise SystemExit("两次输入不一致")
    problem = users_db.password_problem(username, first)
    if problem:
        raise SystemExit(problem)
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="新建账号")
    create.add_argument("--username", required=True)
    create.add_argument("--role", default="viewer", choices=users_db.ROLES)

    passwd = sub.add_parser("passwd", help="重设某个账号的口令")
    passwd.add_argument("--username", required=True)

    role = sub.add_parser("role", help="改角色")
    role.add_argument("--username", required=True)
    role.add_argument("--role", required=True, choices=users_db.ROLES)

    disable = sub.add_parser("disable", help="停用（不删除，审计记录仍指向这个人）")
    disable.add_argument("--username", required=True)
    enable = sub.add_parser("enable", help="恢复启用")
    enable.add_argument("--username", required=True)

    sub.add_parser("list", help="列出所有账号")

    args = parser.parse_args()
    db = users_db.connect()
    try:
        if args.command == "list":
            print(json.dumps(users_db.listing(db), ensure_ascii=False, indent=2))
            return
        if args.command == "create":
            users_db.create(db, args.username, ask_password(args.username), args.role,
                            by=f"命令行（{getpass.getuser()}）", hasher=hash_password)
            print(f"已建：{args.username}（{users_db.ROLE_LABELS[args.role]}）")
            return
        if args.command == "passwd":
            users_db.set_password(db, args.username, ask_password(args.username),
                                  hasher=hash_password)
            print(f"已改口令：{args.username}")
            return
        if args.command == "role":
            users_db.set_role(db, args.username, args.role)
            print(f"{args.username} → {users_db.ROLE_LABELS[args.role]}")
            return
        if args.command in ("disable", "enable"):
            users_db.set_active(db, args.username, args.command == "enable")
            print(f"{args.username} 已{'启用' if args.command == 'enable' else '停用'}")
    except ValueError as exception:
        print(f"做不了：{exception}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
