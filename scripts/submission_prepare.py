#!/usr/bin/env python3
"""Prepare consistent snapshots and fresh submission accounts, never export live passwords.

Run snapshot in the source environment; run sanitize on the copied database in the submission
checkout. The live database is only read for snapshot. provision-live inserts
the explicitly requested competition account without changing existing users.
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def credentials():
    private = ROOT / ".private"
    private.mkdir(exist_ok=True)
    path = private / "accounts.json"
    if path.exists() and not (ROOT / "ACCESS.md").exists():
        return json.loads(path.read_text())
    access = ROOT / 'ACCESS.md'
    if access.exists():
        # A cloned submission already has matching fixture credentials in its
        # private ACCESS.md. Import them rather than silently rotate passwords
        # only in the JSON while the shipped database still uses the old ones.
        imported=[]
        for line in access.read_text().splitlines():
            parts=[part.strip() for part in line.strip('|').split('|')]
            if len(parts)==4 and parts[0].startswith('competition-'):
                imported.append(dict(username=parts[0],role=parts[1],password=parts[2]))
        if len(imported)!=5 or {a['role'] for a in imported}!={'viewer','analyst','reviewer','admin'}:
            raise SystemExit('ACCESS.md credential table is incomplete; refusing password rotation.')
        path.write_text(json.dumps(imported,indent=2))
        os.chmod(path,0o600)
        print('Private test credential JSON imported from ACCESS.md; no password rotation.')
        return imported
    accounts = [{"username": f"competition-{name}", "role": role,
                 "password": secrets.token_urlsafe(14)} for name, role in
                [("viewer", "viewer"), ("analyst", "analyst"),
                 ("analyst2", "analyst"), ("reviewer", "reviewer"), ("admin", "admin")]]
    path.write_text(json.dumps(accounts, indent=2))
    os.chmod(path, 0o600)
    access = ROOT / "ACCESS.md"
    lines = ["# 本地演示账号", "", "以下为本地虚构身份；线上评审凭据通过比赛指定渠道单独提供。", "",
             "本地入口：http://127.0.0.1:8000/；线上地址通过比赛指定渠道单独提供。", "",
             "全部账号仅用于本地部署与独立测试，不可作为线上登录凭据。", "",
             "| 账号 | 角色 | 密码 | 可用环境 |", "|---|---|---|---|"]
    for a in accounts:
        env = "本地演示"
        lines.append(f"| {a['username']} | {a['role']} | {a['password']} | {env} |")
    lines += ["", "赛后通过账号管理停用比赛账号；现有生产账号及其密码不随包分发。",
              "公网测试以 HTTPS 入口为准。多角色协同与压力写入仅在独立测试数据库执行。"]
    access.write_text("\n".join(lines) + "\n")
    print("Fresh submission credentials prepared; passwords omitted from console.")
    return accounts


def snapshot(source: Path, target: Path):
    target.mkdir(parents=True, exist_ok=False)
    for name in ("collector.sqlite3", "benchmark.sqlite3"):
        with sqlite3.connect(f"file:{source / name}?mode=ro", uri=True) as src:
            with sqlite3.connect(target / name) as dst:
                src.backup(dst)
    print("Consistent snapshot created:", target)


def sanitize():
    from api.auth import hash_password
    from api import users_db
    accounts = credentials()
    db = sqlite3.connect(ROOT / "data/state/collector.sqlite3")
    db.row_factory = sqlite3.Row
    # Only the copied database is changed. Remove human workflow/audit records
    # and every live password hash while retaining machine analysis results.
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cleared = []
    for name in ("platform_users", "platform_actions", "review_tasks", "review_decisions",
                 "review_assignments", "review_pre_annotations", "model_gateway_state", "alert_subscriptions", "alert_deliveries",
                 "alert_dispositions", "alert_disposition_events", "notification_reads",
                 "notifications", "notification_items", "issue_adjustments", "issue_adjustment_events"):
        if name in tables:
            db.execute(f'DELETE FROM "{name}"')
            cleared.append(name)
    db.commit()
    users_db.ensure_schema(db)
    for a in accounts:
        users_db.create(db, **a, by="competition submission bootstrap", hasher=hash_password)
    db.execute("PRAGMA journal_mode=DELETE")
    # VACUUM discards deleted live hashes from free pages in the shipped file.
    db.execute("VACUUM")
    result = {"cleared_tables": cleared, "accounts": [a["username"] for a in accounts],
              "integrity_check": db.execute("PRAGMA integrity_check").fetchone()[0]}
    db.close()
    (ROOT / ".private/sanitization.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


def provision_live():
    from api import users_db
    from api.auth import hash_password
    account = json.load(sys.stdin)
    if account["username"] != "competition-reviewer" or account["role"] != "reviewer":
        raise SystemExit("Only the requested competition-reviewer may be provisioned live.")
    with users_db.connect() as db:
        if users_db.row_of(db, account["username"]):
            raise SystemExit("Account already exists; do not overwrite its password.")
        users_db.create(db, **account, by="competition submission", hasher=hash_password)
    print("competition-reviewer created; existing production accounts unchanged.")


def load_accounts():
    """Create additional identities on an isolated copy only, never shipped."""
    from api import users_db
    from api.auth import hash_password
    if 'lcei-submission-test-' not in str(ROOT):
        raise SystemExit('Load identities may only be created in the isolated test checkout.')
    accounts = credentials()
    with users_db.connect() as db:
        for i in range(50):
            account = dict(username=f'competition-load-{i:02d}', role='analyst',
                           password=secrets.token_urlsafe(14))
            users_db.create(db, **account, by='isolated load bootstrap', hasher=hash_password)
            accounts.append(account)
    path = ROOT / '.private/accounts.json'
    path.write_text(json.dumps(accounts, indent=2))
    os.chmod(path, 0o600)
    print('50 isolated load identities created; secrets omitted.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["credentials", "snapshot", "sanitize", "provision-live", "load-accounts"])
    p.add_argument("--source", type=Path)
    p.add_argument("--target", type=Path)
    p.add_argument('--confirm-disposable-copy', action='store_true',
                   help='Required before deleting copied human/account records; never use against production.')
    a = p.parse_args()
    if a.mode == "credentials": credentials()
    elif a.mode == "snapshot": snapshot(a.source, a.target)
    elif a.mode == "sanitize":
        if not a.confirm_disposable_copy:
            raise SystemExit('Sanitization deletes copied human records. Confirm this is a disposable copy with --confirm-disposable-copy.')
        sanitize()
    elif a.mode == 'load-accounts': load_accounts()
    else: provision_live()

if __name__ == "__main__":
    main()
