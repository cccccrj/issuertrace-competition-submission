"""Subscriptions, the outbox, and what was decided about an alert.

Separate from ``api/notify_db.py`` on purpose. That module owns the *inbox* — a
record that a person was shown something inside the platform. This one owns
*dispatch*: rules about who should be told, attempts to tell them through a
channel that leaves the machine, and the handling decision that follows. They
share a database and nothing else; conflating them would put an outbound
delivery attempt in the same table as a read receipt.

The same arrangement as ``api/review_db.py``, and for the same reason: these are
facts recorded *about* the analysis, never edits of it. Re-running the pipeline
must not erase who was told what.

Three properties are built into the schema rather than left to the code:

* **A suppressed delivery is a row.** Not sending is a decision with a reason,
  and a decision nobody can see is indistinguishable from a bug. The outbox
  records ``suppressed`` alongside ``sent``, with the rule that fired.
* **Secrets are referenced, never stored.** ``secret_ref`` holds the *name* of
  an environment variable. A database that cannot leak a signing key is better
  than one that is careful with it.
* **Dry-run is the default.** A subscription starts life unable to open a
  socket, and leaving that state is a deliberate act with a role behind it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from api import dispositions
from api.db import DB_PATH, has_column

CHANNELS = ("inapp", "email", "webhook")
STATUSES = ("queued", "dry_run", "sent", "failed", "suppressed")
#: 取自 ``api.dispositions.STATUSES``，避免同一组状态在两处各写一遍。
DISPOSITION_STATUSES = tuple(dispositions.STATUSES)

#: 旧码 → 业务码。``acting``/``done`` 是纯技术命名，界面上要显示「核查中」
#: 「待上报」，两者之间隔着一层翻译——而翻译层正是一个量长出第二个名字的地方。
#: 迁移写在 ensure_schema 里跟着建表一起跑，幂等：库里当前 401 行全是 pending，
#: 这条 UPDATE 命中 0 行，但它必须在，否则任何一个旧库升上来都会有读不出标签的行。
LEGACY_DISPOSITION_STATUS = {"acting": "investigating", "done": "reporting"}

#: 人工研判的三列，处置表与流水表各补一份。
JUDGEMENT_COLUMNS = (("conclusion", "TEXT"), ("level_override", "TEXT"), ("opinion", "TEXT"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    # Shares the database with the processing layer, so it needs the busy
    # timeout review_db needs: a push queued while a run holds the write lock
    # should wait its turn rather than fail with "database is locked".
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.execute("PRAGMA busy_timeout=30000")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS alert_subscriptions(
        subscription_id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
        company_id TEXT, issue_code TEXT,
        min_risk_level TEXT NOT NULL DEFAULT 'high', min_credibility TEXT,
        phases_json TEXT NOT NULL DEFAULT '[]',
        reason_codes_json TEXT NOT NULL DEFAULT '[]',
        channel TEXT NOT NULL, target TEXT,
        -- The NAME of an environment variable, never the value. A database that
        -- cannot leak a signing key beats one that is careful with it.
        secret_ref TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        dry_run INTEGER NOT NULL DEFAULT 1,
        quiet_hours TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS alert_deliveries(
        delivery_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, alert_id TEXT NOT NULL,
        company_id TEXT, issue_code TEXT,
        reason TEXT NOT NULL, channel TEXT NOT NULL, target TEXT,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL, suppressed_reason TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        http_status INTEGER, response_excerpt TEXT, error TEXT,
        queued_at TEXT NOT NULL, attempted_at TEXT, delivered_at TEXT,
        digest TEXT NOT NULL)""")
    # One row per (subscription, alert, reason, payload). Re-running evaluation
    # is therefore safe: an unchanged alert produces the same digest and is
    # ignored, while a changed one is a genuinely new row.
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS alert_deliveries_once
        ON alert_deliveries(subscription_id, alert_id, reason, digest)""")
    db.execute("""CREATE INDEX IF NOT EXISTS alert_deliveries_issue
        ON alert_deliveries(subscription_id, issue_code, queued_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS alert_dispositions(
        disposition_id TEXT PRIMARY KEY, alert_id TEXT NOT NULL UNIQUE, company_id TEXT,
        recommended TEXT NOT NULL, recommended_basis_json TEXT NOT NULL,
        chosen TEXT, status TEXT NOT NULL DEFAULT 'pending',
        note TEXT, actor TEXT, role TEXT, decided_at TEXT, created_at TEXT NOT NULL)""")
    # 三列补在后面而不是重建表：``decided_at IS NOT NULL`` 的行是人做过的决定，
    # 重建表等于把它们抄一遍，而抄一遍就有抄错的可能。
    #
    # issue_code 与 window_start 不是冗余——alert_id 是 sha256(公司‖议题‖窗口起点)，
    # 而 processor.alerts 每轮 DELETE 后重建，窗口起点一漂移 id 就换一个，人判过的
    # 那一行当场变成孤儿（库里现存 125 条就是这么来的）。存下这三项，重算之后
    # 还能按业务键把它认回来。
    # due_at 是专项核查唯一需要、而处置表原本没有的字段：清单第 3 项「不建第二套任务实体」
    # 的全部代价就是这一列。核查的状态、责任人、系统建议、人工选择、说明、流水全都已经在这里。
    #
    # conclusion / level_override / opinion 是人工研判那三样（修改思路 v2 §五）：风险成不成立、
    # 人认为该定几级、依据是什么。level_override 单独一列而不是改写 alerts.risk_level——
    # 那一列是处理层的产物，重跑就换一遍；人的定级是第二层事实，与机器的并排放着才比得出分歧。
    for column, ddl in (("issue_code", "TEXT"), ("window_start", "TEXT"), ("owner", "TEXT"),
                        ("due_at", "TEXT"), *JUDGEMENT_COLUMNS):
        if not has_column(db, "alert_dispositions", column):
            db.execute(f"ALTER TABLE alert_dispositions ADD COLUMN {column} {ddl}")
    db.execute("""CREATE TABLE IF NOT EXISTS alert_disposition_events(
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_id TEXT NOT NULL, company_id TEXT, issue_code TEXT, window_start TEXT,
        from_status TEXT, to_status TEXT NOT NULL,
        chosen TEXT, recommended TEXT, owner TEXT, note TEXT,
        actor TEXT NOT NULL, role TEXT NOT NULL, occurred_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS alert_disposition_events_alert
        ON alert_disposition_events(alert_id, event_seq)""")
    # 截止时间改了也要进流水：「谁把期限从周五推到了下周三」和「谁把状态改成了核查中」
    # 是同一类事实。
    for column, ddl in (("due_at", "TEXT"), *JUDGEMENT_COLUMNS):
        if not has_column(db, "alert_disposition_events", column):
            db.execute(f"ALTER TABLE alert_disposition_events ADD COLUMN {column} {ddl}")
    for legacy, current in LEGACY_DISPOSITION_STATUS.items():
        db.execute("UPDATE alert_dispositions SET status=? WHERE status=?", (current, legacy))
    db.commit()


def digest_of(payload: dict[str, Any]) -> str:
    """A stable fingerprint of what the recipient would see.

    Keyed on the payload rather than a timestamp, so "have we already said this"
    is answerable from the thing that was actually said.
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def subscriptions(db: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    clause = " WHERE enabled=1" if enabled_only else ""
    return [dict(row) for row in db.execute(
        f"SELECT * FROM alert_subscriptions{clause} ORDER BY created_at DESC")]


def recent_deliveries(db: sqlite3.Connection, subscription_id: str, issue_code: str | None,
                      limit: int = 20) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(
        """SELECT * FROM alert_deliveries
           WHERE subscription_id=? AND (issue_code IS ? OR issue_code=?)
           ORDER BY queued_at DESC LIMIT ?""",
        (subscription_id, issue_code, issue_code, limit))]


def queue(db: sqlite3.Connection, subscription: dict[str, Any], alert: dict[str, Any],
          reason: str, body: dict[str, Any], *, suppressed: str | None = None) -> str | None:
    """Record an intended delivery — including one that will never be sent.

    Returns the delivery id, or ``None`` when this exact payload was already
    queued for this reason, which is what makes re-evaluating idempotent.
    """
    fingerprint = digest_of(body)
    identifier = hashlib.sha256(
        f"{subscription['subscription_id']}\0{alert['alert_id']}\0{reason}\0{fingerprint}"
        .encode("utf-8")).hexdigest()[:24]
    status = "suppressed" if suppressed else ("dry_run" if subscription.get("dry_run", 1) else "queued")
    try:
        db.execute("""INSERT INTO alert_deliveries
            (delivery_id, subscription_id, alert_id, company_id, issue_code, reason, channel,
             target, payload_json, status, suppressed_reason, queued_at, digest)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (identifier, subscription["subscription_id"], alert["alert_id"],
             alert.get("company_id"), alert.get("issue_code"), reason, subscription["channel"],
             subscription.get("target"), json.dumps(body, ensure_ascii=False),
             status, suppressed, utc_now(), fingerprint))
    except sqlite3.IntegrityError:
        return None
    db.commit()
    return identifier


def record_attempt(db: sqlite3.Connection, delivery_id: str, *, status: str,
                   http_status: int | None = None, response: str | None = None,
                   error: str | None = None) -> None:
    db.execute("""UPDATE alert_deliveries
        SET status=?, attempts=attempts+1, attempted_at=?,
            delivered_at=CASE WHEN ?='sent' THEN ? ELSE delivered_at END,
            http_status=?, response_excerpt=?, error=?
        WHERE delivery_id=?""",
        (status, utc_now(), status, utc_now(), http_status,
         (response or "")[:400] or None, (error or "")[:400] or None, delivery_id))
    db.commit()


def disposition_for(db: sqlite3.Connection, alert_id: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM alert_dispositions WHERE alert_id=?", (alert_id,)).fetchone()
    return dict(row) if row else None


def dispositions_by_alert(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every stored disposition in one read, for the loop that needs them all."""
    return {row["alert_id"]: dict(row)
            for row in db.execute("SELECT * FROM alert_dispositions")}


def prune_stale_recommendations(db: sqlite3.Connection, *, commit: bool = True) -> int:
    """删掉指向已不存在事项的**纯建议**行，并且只删这一种。

    这些行是 alert_id 漂移留下的：`processor.alerts` 曾经把窗口起点算进 id，回填一篇
    更早的报道就换一个 id，上一轮的建议行当场没了对应的事项。2026-09-22 起 id 由
    上一轮认领延续（`processor.alerts.carry_forward`），新的孤儿不会再产生；这条函数
    收拾的是那之前留下的 125 行。

    删除条件里三个 NULL 一个都不能少：**人碰过的一行永远不删**。一条有人判过却找不到
    事项的记录是个需要有人来看的问题，不是垃圾——把它悄悄删掉，就等于把「我们弄丢过
    一条人工结论」这件事也一起删掉了。
    """
    cursor = db.execute("""DELETE FROM alert_dispositions
        WHERE chosen IS NULL AND decided_at IS NULL AND actor IS NULL AND status = 'pending'
          AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.alert_id = alert_dispositions.alert_id)""")
    if commit:
        db.commit()
    return cursor.rowcount


def upsert_disposition(db: sqlite3.Connection, alert_id: str, company_id: str | None,
                       recommendation: dict[str, Any], *, existing: dict[str, Any] | None = None,
                       commit: bool = True) -> dict[str, Any]:
    """Store the recommendation without touching a choice a person already made.

    The recommendation is derived and may change when the alert is rebuilt; the
    chosen action is a human decision and must not be overwritten by one.

    ``commit`` and ``existing`` exist for the one caller that does this in a loop.
    ``/notify/dispositions`` re-derives every open alert's recommendation on each
    page open, and committing per row made the event detail page cost 1.24 s —
    277 fsyncs and 554 point reads for a page that shows one alert's advice.  The
    row still lands; it lands once at the end of the loop.
    """
    existing = disposition_for(db, alert_id) if existing is None else existing
    basis = json.dumps(recommendation["basis"], ensure_ascii=False)
    if existing:
        db.execute("""UPDATE alert_dispositions SET recommended=?, recommended_basis_json=?
            WHERE alert_id=?""", (recommendation["recommended"], basis, alert_id))
        stored = {**existing, "recommended": recommendation["recommended"],
                  "recommended_basis_json": basis}
    else:
        disposition_id = hashlib.sha256(alert_id.encode()).hexdigest()[:24]
        created_at = utc_now()
        db.execute("""INSERT INTO alert_dispositions
            (disposition_id, alert_id, company_id, recommended, recommended_basis_json, created_at)
            VALUES(?,?,?,?,?,?)""",
            (disposition_id, alert_id, company_id, recommendation["recommended"], basis, created_at))
        stored = disposition_for(db, alert_id)
    if commit:
        db.commit()
    return stored


class IllegalTransition(ValueError):
    """流程不允许这一步。带上合法去向，好让拒绝这件事本身是可操作的。"""

    def __init__(self, current: str, target: str, allowed: tuple[str, ...]):
        self.current, self.target, self.allowed = current, target, allowed
        names = "、".join(dispositions.STATUSES[name]["label"] for name in allowed) or "无"
        super().__init__(
            f"「{dispositions.STATUSES.get(current, {}).get('label', current)}」"
            f"不能直接转为「{dispositions.STATUSES[target]['label']}」；可去往：{names}")


#: `decide(due_at=KEEP)` 表示「截止时间不动」，与「清空截止时间」（None）分开——
#: 否则每一次只改状态的提交，都会把之前定好的期限悄悄抹掉。
KEEP = object()


def submitter_of(db: sqlite3.Connection, alert_id: str) -> str | None:
    """最近一次把这条事项**送进**待上级复核的人——退回时负责人回到他手上。"""
    row = db.execute("""SELECT actor FROM alert_disposition_events
        WHERE alert_id=? AND to_status='reporting' AND coalesce(from_status,'') <> 'reporting'
        ORDER BY event_seq DESC LIMIT 1""", (alert_id,)).fetchone()
    return row["actor"] if row else None


def decide(db: sqlite3.Connection, alert_id: str, *, chosen: str | None, status: str,
           note: str | None, actor: str, role: str, owner: str | None = None,
           context: dict[str, Any] | None = None, due_at: Any = KEEP,
           conclusion: Any = KEEP, level_override: Any = KEEP, opinion: Any = KEEP) -> dict[str, Any] | None:
    """Move an alert one legal step along the flow, and record the step.

    两件事必须同时发生，所以放在一个事务里：当前状态换新，以及往
    ``alert_disposition_events`` 追加一行说明这一步是谁在什么时候走的。少了后者，
    「这条事项经历过什么」就只能从最后一次覆盖的结果去猜——而那正是 review 队列
    早就拒绝过的形状（`api/review_db.py` 开头那段：记录的是关于裁决的第二层事实，
    不是对裁决的编辑）。

    追加的那一行同时存下当时的 ``recommended``，于是「人机分歧率」是可以事后算的，
    不必在判的时候就决定要不要统计它。

    ``status`` 等于当前状态也是一步：认领（只换负责人）、保存研判结论（只写结论）
    都是这样走的，同样留一行流水。``chosen`` 为 None 表示不动已选的口径——认领
    一条事项不等于替它选了对外口径。

    退回（→ ``returned``）时负责人自动回到最近一次上报的人：退回意见是写给他的，
    交给别人等于没退回。

    Raises:
        IllegalTransition: 目标状态不在当前状态的合法去向里。
    """
    existing = disposition_for(db, alert_id) or {}
    current = existing.get("status") or "pending"
    if status != current and not dispositions.can_transition(current, status):
        raise IllegalTransition(current, status, dispositions.TRANSITIONS.get(current, ()))
    context = context or {}
    now = utc_now()

    def kept(value: Any, column: str) -> Any:
        return existing.get(column) if value is KEEP else value

    due = kept(due_at, "due_at")
    judged = {"conclusion": kept(conclusion, "conclusion"),
              "level_override": kept(level_override, "level_override"),
              "opinion": kept(opinion, "opinion")}
    chosen = chosen or existing.get("chosen")
    if status == "returned" and current != "returned":
        owner = submitter_of(db, alert_id) or owner
    owner = owner or existing.get("owner")
    db.execute("""UPDATE alert_dispositions
        SET chosen=?, status=?, note=?, actor=?, role=?, decided_at=?, owner=?, due_at=?,
            conclusion=?, level_override=?, opinion=?,
            issue_code=coalesce(?, issue_code), window_start=coalesce(?, window_start)
        WHERE alert_id=?""",
        (chosen, status, note, actor, role, now, owner, due,
         judged["conclusion"], judged["level_override"], judged["opinion"],
         context.get("issue_code"), context.get("window_start"), alert_id))
    db.execute("""INSERT INTO alert_disposition_events
        (alert_id, company_id, issue_code, window_start, from_status, to_status,
         chosen, recommended, owner, note, actor, role, occurred_at, due_at,
         conclusion, level_override, opinion)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (alert_id, existing.get("company_id"),
         context.get("issue_code") or existing.get("issue_code"),
         context.get("window_start") or existing.get("window_start"),
         current, status, chosen, existing.get("recommended"),
         owner, note, actor, role, now, due,
         judged["conclusion"], judged["level_override"], judged["opinion"]))
    db.commit()
    return disposition_for(db, alert_id)


def disposition_events(db: sqlite3.Connection, *, alert_id: str | None = None,
                       to_status: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """处置流水，最近的在前。只读——写入只发生在 ``decide``。

    ``to_status`` 只取「走进」这些状态的那几步（停在原地的更新不算），上报与推送记录页
    用它列出内部上报与退回。
    """
    clauses, parameters = [], []
    if alert_id:
        clauses.append("alert_id=?")
        parameters.append(alert_id)
    if to_status:
        clauses.append(f"to_status IN ({','.join('?' * len(to_status))})"
                       " AND coalesce(from_status,'') <> to_status")
        parameters.extend(to_status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return [dict(row) for row in db.execute(
        f"""SELECT * FROM alert_disposition_events {where}
            ORDER BY event_seq DESC LIMIT ?""", [*parameters, limit])]
