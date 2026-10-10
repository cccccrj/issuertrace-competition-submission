"""The notification inbox: platform state, kept apart from the analysis tables.

An alert is a conclusion the processing layer reached; a notification is the
record that a person was told about it and whether they have read it.  Mixing
the two would make re-running the pipeline erase who saw what, so the inbox has
its own tables next to the review queue, in the platform's own write path.

Notifications are derived, not authored: every one of them points at an alert or
a review task that already exists, and the derivation is idempotent, so a rebuild
after a pipeline run adds what is new without duplicating what was already
delivered or re-raising what somebody has read.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

from api.db import DB_PATH, has_column, has_table

KINDS = ("alert", "review_task", "handoff")
# Alerts below this level are visible on their own page; pushing every one of
# them into an inbox is how an inbox stops being read.
NOTIFIED_RISK_LEVELS = ("high", "medium")


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.execute("PRAGMA busy_timeout=30000")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS notifications(
        notification_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        ref_id TEXT NOT NULL,
        company_id TEXT,
        level TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        link TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(kind, ref_id))""")
    db.execute("CREATE INDEX IF NOT EXISTS notifications_created ON notifications(created_at)")
    # 收件人。此前每条通知都发给所有人——预警与复核任务本来就是全组共享的队列。交接
    # 不是：「退回给你补证」只该出现在那一个人的通知箱里。NULL 仍然是「所有人」。
    if not has_column(db, "notifications", "recipient"):
        db.execute("ALTER TABLE notifications ADD COLUMN recipient TEXT")
    # Read state is per person: two analysts share a queue, not an inbox.
    db.execute("""CREATE TABLE IF NOT EXISTS notification_reads(
        notification_id TEXT NOT NULL,
        username TEXT NOT NULL,
        read_at TEXT NOT NULL,
        PRIMARY KEY(notification_id, username))""")
    db.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def notification_id(kind: str, ref_id: str) -> str:
    return hashlib.sha256(f"notify\0{kind}\0{ref_id}".encode()).hexdigest()[:20]


def sync(db: sqlite3.Connection, analysis: sqlite3.Connection) -> int:
    """Bring the inbox up to date with the alerts and the review queue.

    Runs on read rather than on a timer: the platform has no scheduler of its
    own, and an inbox that only fills when someone happens to run a job is worse
    than one built from the tables it mirrors.
    """
    rows: list[tuple[str, ...]] = []
    for alert in analysis.execute("""SELECT alert_id, company_id, risk_level, headline, summary,
                                            created_at FROM alerts WHERE risk_level IN (?, ?)""",
                                  NOTIFIED_RISK_LEVELS):
        rows.append((notification_id("alert", alert["alert_id"]), "alert", alert["alert_id"],
                     alert["company_id"], alert["risk_level"], alert["headline"],
                     alert["summary"][:400], f"/risks/{alert['alert_id']}",
                     alert["created_at"]))
    for task in db.execute("""SELECT task_id, kind, company_id, origin, created_at
        FROM review_tasks WHERE status='open'"""):
        rows.append((notification_id("review_task", task["task_id"]), "review_task",
                     task["task_id"], task["company_id"], "task",
                     {"verdict": "待复核：主张裁决", "event": "待复核：事件抽取",
                      "diff": "待复核：披露差异"}.get(task["kind"], "待复核"),
                     f"入列理由：{task['origin']}", "/review", task["created_at"]))
    before = db.execute("SELECT count(*) FROM notifications").fetchone()[0]
    db.executemany("""INSERT OR IGNORE INTO notifications
        (notification_id,kind,ref_id,company_id,level,title,body,link,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""", rows)
    db.executemany("""INSERT OR IGNORE INTO notifications
        (notification_id,kind,ref_id,company_id,level,title,body,link,created_at,recipient)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", handoffs(db, analysis))
    # 预警通知此前链到 /alerts?alert=…——那一页退役后它重定向到事项库首页，点通知的人
    # 落在一张 277 行的表上，还得自己找那一条。改成直达那条事项；已存的行一并改掉。
    db.execute("""UPDATE notifications SET link='/risks/'||ref_id
        WHERE kind='alert' AND link LIKE '/alerts%'""")
    # A task that has been judged is no longer waiting for anyone; leaving it in
    # the inbox would make the unread count a number nobody can bring down.
    db.execute("""DELETE FROM notifications WHERE kind='review_task' AND ref_id NOT IN
        (SELECT task_id FROM review_tasks WHERE status='open')""")
    db.commit()
    return db.execute("SELECT count(*) FROM notifications").fetchone()[0] - before


#: 交接通知的三种说法。键是 ``handoff_kind`` 的结果。
HANDOFF_TITLES = {
    "reporting": "上报给你复核",
    "returned": "退回给你补证",
    "assigned": "分派给你",
}


def handoff_kind(step: dict[str, Any], previous_owner: str | None) -> str | None:
    """这一步是不是把事项交到了另一个人手上；是的话是哪一种。

    只看**负责人换成了别人**的那几步：自己认领自己不必通知自己；负责人没变的一步
    （保存结论、改截止日）不是交接。上报与退回即使负责人恰好没变（重复上报给同一个人）
    也算——那是对方要处理的新一轮。
    """
    owner, actor = step["owner"], step["actor"]
    if not owner or owner == actor:
        return None
    moved = step["from_status"] != step["to_status"]
    if moved and step["to_status"] in ("reporting", "returned"):
        return step["to_status"]
    return "assigned" if owner != previous_owner else None


def handoffs(db: sqlite3.Connection, analysis: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """从处置流水里读出每一次交接，一次交接一条通知，只发给接手的那个人。

    与预警、复核任务一样是**推导**出来的，不是另写的：流水是唯一的事实，通知是它的一个
    读法，重复同步不会重复发（``UNIQUE(kind, ref_id)``，ref_id 是流水序号）。
    """
    if not has_table(db, "alert_disposition_events"):
        return []
    headlines = {row["alert_id"]: row["headline"] for row in analysis.execute(
        "SELECT alert_id, headline FROM alerts")}
    rows, owners = [], {}
    for step in db.execute("""SELECT event_seq, alert_id, company_id, from_status, to_status,
                                     owner, actor, note, occurred_at
                              FROM alert_disposition_events ORDER BY alert_id, event_seq"""):
        step = dict(step)
        kind = handoff_kind(step, owners.get(step["alert_id"]))
        owners[step["alert_id"]] = step["owner"] or owners.get(step["alert_id"])
        if not kind:
            continue
        headline = headlines.get(step["alert_id"], "（事项已被重算替换）")
        body = f"{step['actor']}：{step['note']}" if step["note"] else f"来自 {step['actor']}"
        rows.append((notification_id("handoff", str(step["event_seq"])), "handoff",
                     str(step["event_seq"]), step["company_id"], "task",
                     f"{HANDOFF_TITLES[kind]}：{headline}", body[:400],
                     f"/risks/{step['alert_id']}", step["occurred_at"], step["owner"]))
    return rows


def inbox(db: sqlite3.Connection, username: str, *, unread_only: bool = False,
          limit: int = 50, offset: int = 0) -> dict[str, Any]:
    visible = "(n.recipient IS NULL OR n.recipient=?)"
    clause = f"WHERE {visible}" + (" AND r.read_at IS NULL" if unread_only else "")
    rows = db.execute(f"""SELECT n.*, r.read_at FROM notifications n
        LEFT JOIN notification_reads r
          ON r.notification_id=n.notification_id AND r.username=?
        {clause} ORDER BY n.created_at DESC, n.notification_id LIMIT ? OFFSET ?""",
        (username, username, limit, offset)).fetchall()
    total = db.execute(f"""SELECT count(*) FROM notifications n
        LEFT JOIN notification_reads r
          ON r.notification_id=n.notification_id AND r.username=?
        {clause}""", (username, username)).fetchone()[0]
    unread = db.execute(f"""SELECT count(*) FROM notifications n
        LEFT JOIN notification_reads r
          ON r.notification_id=n.notification_id AND r.username=?
        WHERE {visible} AND r.read_at IS NULL""", (username, username)).fetchone()[0]
    return {"total": total, "unread": unread, "limit": limit, "offset": offset,
            "items": [dict(row) for row in rows]}


def mark_read(db: sqlite3.Connection, username: str, ids: list[str] | None) -> int:
    """Mark some notifications read, or all of the ones this person can see when ids is None."""
    targets = ids if ids is not None else [
        row[0] for row in db.execute(
            "SELECT notification_id FROM notifications WHERE recipient IS NULL OR recipient=?",
            (username,))]
    db.executemany("INSERT OR IGNORE INTO notification_reads VALUES(?,?,?)",
                   [(item, username, now()) for item in targets])
    db.commit()
    return len(targets)
