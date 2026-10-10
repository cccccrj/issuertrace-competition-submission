"""The audit trail, read-only: what ran, what a person decided, what was billed.

M0-2 asked for an audit log.  Every part of it already existed — processing runs
in their table, human decisions in theirs, collection events in the JSONL the
collector appends to, and the model and prompt version stamped on every verdict
— with no way to look at any of it without a shell.  This is that one place, and
it adds no new record: a page that could write to the audit trail would not be
an audit trail.

平台操作（``/actions``）是第七类，也是唯一一类**为了留痕而存在**的记录：登录、导出、
权限变更都不改变任何业务数据，没有别的表会顺带记下它们。写它的仍然不是这里——是
``api/platform_audit.record()``，由动作发生的那条路由调用。这一页只读。

处置流水（``/dispositions``）是第六类，遵守同一条规矩：写它的是业务动作自己
（``POST /api/notify/dispositions/{alert_id}``），因为「谁在什么时候把这条事项转成了
核查中」是**处置这件事的本体记录**，不是为了审计才写的日志——与 ``review_decisions``
同一性质。这里只读。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from api import dispatch_db, dispositions, platform_audit, review_db
from api.errors import RUN_STATUS_LABELS, classify, outcome_label, reference
from api.sources import source_name
from api.routes.auth import current_session
from api.routes.events import db_session
from collector.collect import AUDIT_LOG

router = APIRouter(prefix="/api/audit", tags=["audit"])

# The collection log is appended to for months and is read from the end.  Two
# megabytes is a handful of pages; reading the whole file to show fifty lines
# would be the kind of thing that only hurts once the corpus is real.
TAIL_BYTES = 2_000_000


@router.get("/runs")
def runs(limit: int = Query(50, ge=1, le=200), db=Depends(db_session),
         session=Depends(current_session)) -> dict[str, Any]:
    rows = db.execute("""SELECT run_id, started_at, completed_at, status,
                                documents_seen, documents_processed, ai_documents, error_message
        FROM processing_runs ORDER BY started_at DESC LIMIT ?""", (limit,)).fetchall()
    # 原始报错、批次内部编号（里面带着研判程序与提示词版本）、处理器版本与运行参数都留在
    # 库里，不出这个接口（I-06）。读者看到的是类别和一个可以报给运维的短编号。
    return {"items": [{"reference": reference(row["run_id"]), "started_at": row["started_at"],
                       "completed_at": row["completed_at"], "status": row["status"],
                       "status_label": RUN_STATUS_LABELS.get(row["status"], "其他"),
                       "documents_seen": row["documents_seen"],
                       "documents_processed": row["documents_processed"],
                       "analysed_documents": row["ai_documents"],
                       "error": classify(row["error_message"])}
                      for row in rows],
            "total": db.execute("SELECT count(*) FROM processing_runs").fetchone()[0]}


@router.get("/dispositions")
def disposition_events(limit: int = Query(100, ge=1, le=500),
                       alert_id: str | None = None,
                       to_status: list[str] | None = Query(None),
                       session=Depends(current_session)) -> dict[str, Any]:
    """风险事项的状态流转，最近的在前。

    与 ``/decisions`` 并列而不是合并：复核判定是「模型判得对不对」的第二层事实，
    处置流转是「这条风险后来怎么办了」的第二层事实，两者的主语不是一回事。

    每行都带上当时的系统建议，所以「人有没有照建议做」是可以直接读出来的，
    不必回到别的页面去对。
    """
    db = dispatch_db.connect()
    try:
        rows = dispatch_db.disposition_events(db, alert_id=alert_id, to_status=to_status, limit=limit)
        total = db.execute("SELECT count(*) FROM alert_disposition_events").fetchone()[0]
        # 流水里只有 alert_id；读的人要的是「哪一条事项」。事项被重算替换时标题缺着，照实说缺。
        ids = sorted({row["alert_id"] for row in rows})
        headlines = {item["alert_id"]: item["headline"] for item in db.execute(
            f"SELECT alert_id, headline FROM alerts WHERE alert_id IN ({','.join('?' * len(ids))})",
            ids)} if ids else {}
    finally:
        db.close()
    for row in rows:
        row["from_label"] = dispositions.STATUSES.get(row["from_status"], {}).get("label")
        row["to_label"] = dispositions.STATUSES.get(row["to_status"], {}).get("label")
        row["headline"] = headlines.get(row["alert_id"])
        row["conclusion_label"] = dispositions.CONCLUSIONS.get(row.get("conclusion"), {}).get("label")
        row["chosen_label"] = dispositions.ACTIONS.get(row["chosen"], {}).get("label")
        row["recommended_label"] = dispositions.ACTIONS.get(row["recommended"], {}).get("label")
        # 这份流水存在的意义就是这一列：人有没有偏离建议，偏离了多少。
        row["diverged"] = bool(row["chosen"]) and row["chosen"] != row["recommended"]
    return {"items": rows, "total": total,
            "statuses": dispositions.STATUSES, "actions": dispositions.ACTIONS}


#: 导出对象的种类，读者看到的名字。
OBJECT_KIND_LABELS = {"event": "事件报告", "issue": "专题报告", "risk": "风险事项报告", "account": "账号"}


def object_labels(analysis, rows: list[dict[str, Any]]) -> None:
    """把导出记录里的内部编号换成对象本身的名字，编号不再出这个接口（I-06）。"""
    queries = {"event": "SELECT summary FROM event_records WHERE event_id=?",
               "risk": "SELECT headline FROM alerts WHERE alert_id=?",
               "issue": "SELECT company_id || '｜' || issue_code FROM issue_ledger WHERE issue_id=?"}
    for row in rows:
        kind, object_id = row.get("object_kind"), row.pop("object_id", None)
        row["object_kind_label"] = OBJECT_KIND_LABELS.get(kind)
        # 账号操作的对象就是账号名——「把谁的角色改了」正是这一行要回答的。
        label = object_id if kind == "account" else None
        if kind in queries and object_id:
            try:
                found = analysis.execute(queries[kind], (object_id,)).fetchone()
                label = found[0] if found else None
            except Exception:  # 旧库少表时只是没有名字，不该拖垮审计页
                label = None
        if kind == "issue" and label:
            from api.labels import ISSUE_CODE_LABELS
            from processor.pipeline import company_display_name
            company, code = label.split("｜", 1)
            label = f"{company_display_name(company)}｜{ISSUE_CODE_LABELS.get(code, code)}"
        row["object_label"] = (label[:60] if label else ("（已不在当前数据中）" if object_id else None))


@router.get("/actions")
def platform_actions(limit: int = Query(100, ge=1, le=500),
                     action: str | None = None, outcome: str | None = None,
                     analysis=Depends(db_session), session=Depends(current_session)) -> dict[str, Any]:
    """登录、导出、权限变更，最近的在前。

    词表跟着行一起返回（``actions``/``outcomes``），前端因此不必自己再抄一份动作名——
    抄一份就是给同一个量起第二个名字，而这一页恰恰是用来对口径的。
    """
    db = platform_audit.connect()
    try:
        rows = platform_audit.recent(db, action=action, outcome=outcome, limit=limit)
        object_labels(analysis, rows)
        total = db.execute("SELECT count(*) FROM platform_actions").fetchone()[0]
        by_action = platform_audit.counts(db)
    finally:
        db.close()
    return {"items": rows, "total": total, "by_action": by_action,
            "actions": platform_audit.ACTION_LABELS, "outcomes": platform_audit.OUTCOME_LABELS}


@router.get("/decisions")
def decisions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              session=Depends(current_session)) -> dict[str, Any]:
    """Who judged what, and what the system had said at that moment."""
    db = review_db.connect()
    try:
        rows = db.execute("""SELECT d.*, t.kind, t.target_id, t.company_id, t.origin
            FROM review_decisions d JOIN review_tasks t ON t.task_id=d.task_id
            ORDER BY d.decided_at DESC, d.decision_id DESC LIMIT ? OFFSET ?""",
            (limit, offset)).fetchall()
        total = db.execute("SELECT count(*) FROM review_decisions").fetchone()[0]
    finally:
        db.close()
    return {"items": [dict(row) for row in rows], "total": total}


@router.get("/events")
def events(result: str | None = None, source: str | None = None,
           limit: int = Query(100, ge=1, le=500),
           session=Depends(current_session)) -> dict[str, Any]:
    """The collector's own log, newest first.

    Filtering happens here rather than in the client because the interesting
    lines — a source isolated, a manual import reviewed — are a handful out of
    tens of thousands.
    """
    if not AUDIT_LOG.exists():
        return {"items": [], "results": [], "scanned": 0, "truncated": False}
    size = AUDIT_LOG.stat().st_size
    with AUDIT_LOG.open("rb") as handle:
        handle.seek(max(0, size - TAIL_BYTES))
        chunk = handle.read().decode("utf-8", errors="ignore")
    lines = chunk.splitlines()
    if size > TAIL_BYTES and lines:
        # The first line of a mid-file read is half a line.
        lines = lines[1:]
    items, results = [], {}
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        shape = outcome_label(event.get("outcome"))
        results[(shape["result"], shape["result_label"])] = results.get((shape["result"], shape["result_label"]), 0) + 1
        if result and shape["result"] != result:
            continue
        if source and event.get("source_id") != source:
            continue
        if len(items) < limit:
            # 采集日志的原行带着本地文件路径、内容哈希、内部任务号和异常原文（I-06），
            # 只取读者需要的几样：何时、哪个来源、做了什么、结果如何、是哪一类失败。
            http_status = event.get("http_status")
            items.append({"at": event.get("at"), "source_id": event.get("source_id"),
                          "source_name": source_name(event.get("source_id")),
                          **shape, "http_status": http_status if isinstance(http_status, int) else None,
                          "url": event.get("final_url") or event.get("url"),
                          "title": event.get("title"),
                          "error": classify(event.get("error") or event.get("reason")
                                            if shape["result"] in ("failure", "attention") else None)})
    return {"items": items, "scanned": len(lines), "truncated": size > TAIL_BYTES,
            "results": sorted(({"value": key[0], "label": key[1], "count": value}
                               for key, value in results.items()), key=lambda row: -row["count"])}
