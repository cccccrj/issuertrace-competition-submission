"""Issue ledger endpoints, and the one correction M4-6 asks for.

「归并错了要能救」.  An issue here is ``(company, issue_code)`` and an event
lands in one because the labelling pass put that code on it; when that is
wrong, the ledger is wrong and nothing could say so.  The correction is a
single primitive — move an event out of a code, into another one or out of the
ledger — with 合并 and 拆分 being the same operation over different event sets.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import issue_db
from api.db import event_clock_sql, has_table, json_field, total_of
from api.routes.auth import current_session, requires
from api.routes.events import db_session
from processor.pipeline import ISSUE_RULES, export_data, rebuild_issue_ledger

router = APIRouter(prefix="/api/issues", tags=["issues"])


def issue_session():
    db = issue_db.connect()
    try:
        yield db
    finally:
        db.close()


def active_map(db) -> dict[tuple[str, str], str | None]:
    """Reassignments in force, keyed by (event, the code it was moved out of)."""
    if not has_table(db, "issue_adjustments"):
        return {}
    return {(row["event_id"], row["from_code"]): row["to_code"]
            for row in db.execute("""SELECT event_id, from_code, to_code
                FROM issue_adjustments WHERE undone_at IS NULL ORDER BY decided_at""")}


def belongs(codes: list[str], event_id: str, issue_code: str,
            adjustments: dict[tuple[str, str], str | None]) -> bool:
    """Whether an event is in this issue *after* the corrections are applied.

    The detail page used to select on ``issue_codes_json LIKE '%"CODE"%'``,
    which reads the raw label and knows nothing about a reassignment — so a
    corrected issue would report one count in the ledger and list a different
    set of events underneath it.
    """
    for code in codes:
        target = adjustments.get((event_id, code), code)
        if target == issue_code:
            return True
    return False


@router.get("")
def list_issues(company: str | None = None, status: str | None = None,
                limit: int = Query(100, ge=1, le=200), db=Depends(db_session)) -> dict[str, Any]:
    filters, parameters = [], []
    if company:
        filters.append("company_id=?")
        parameters.append(company)
    if status:
        filters.append("status=?")
        parameters.append(status)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    rows = db.execute(f"""SELECT * FROM issue_ledger{where}
        ORDER BY risk_score DESC, event_count DESC LIMIT ?""", [*parameters, limit]).fetchall()
    # Counted rather than measured from the page: "total" answers how many
    # issues match the filter, and a page of results cannot answer that.
    return {"total": total_of(db, f"SELECT issue_id FROM issue_ledger{where}", parameters),
            "limit": limit, "items": [dict(row) for row in rows]}


@router.get("/{issue_id}")
def get_issue(issue_id: str, db=Depends(db_session)) -> dict[str, Any]:
    issue = db.execute("SELECT * FROM issue_ledger WHERE issue_id=?", (issue_id,)).fetchone()
    if not issue:
        raise HTTPException(status_code=404, detail="issue not found")
    detail = dict(issue)
    # Every event of this company, not only those whose raw label names this
    # code: an event reassigned *into* the issue carries a different code and
    # would otherwise be invisible on the page that says it is here.
    sort_time, role = event_clock_sql(db)
    events = db.execute(f"""SELECT e.event_id, e.summary, e.event_time, e.risk_level, e.risk_score,
                                  e.needs_review, e.issue_codes_json, {role} AS event_time_role,
                                  {sort_time} AS sort_time,
                                  v.title, v.source_id, v.published_at, v.canonical_url
        FROM event_records e JOIN document_versions v ON v.version_id=e.version_id
        WHERE e.company_id=? AND e.eventhood='EVENT'
        ORDER BY sort_time""",
        (issue["company_id"],)).fetchall()
    # The timeline records which clock each entry used, because mixing an event
    # date with a publication date without saying so misstates the sequence.
    adjustments = active_map(db)
    detail["timeline"] = [{**dict(event),
                           "time_basis": ("event_time" if event["event_time"]
                                          and event["event_time_role"] != "scheduled" else "published_at"),
                           "target_time": (event["event_time"]
                                           if event["event_time_role"] == "scheduled" else None),
                           "reassigned": (event["event_id"], issue["issue_code"]) in adjustments
                           or any((event["event_id"], code) in adjustments
                                  for code in json_field(event["issue_codes_json"]))}
                          for event in events
                          if belongs(json_field(event["issue_codes_json"]), event["event_id"],
                                     issue["issue_code"], adjustments)]
    verdicts = db.execute("""SELECT v.verdict, count(*) AS total FROM claim_verdicts v
        JOIN event_claims c ON c.claim_id=v.claim_id
        JOIN event_records e ON e.event_id=c.event_id
        WHERE e.company_id=? AND c.issue_code=? GROUP BY v.verdict""",
        (issue["company_id"], issue["issue_code"])).fetchall()
    detail["verdicts"] = {row["verdict"]: row["total"] for row in verdicts}
    # An issue's status is a derived word — 新出现 / 升温 / 活跃 / 降温 / 沉寂 —
    # and the ledger keeps only the current one. The trail that produced it is
    # recorded, with the reason and the counts on both sides of each change, and
    # had never been exposed: a reader could see that an issue is 「降温」 but
    # not that it was 「升温」 nine days ago, which is the part that matters.
    detail["status_history"] = []
    if has_table(db, "issue_status_history"):
        detail["status_history"] = [dict(row) for row in db.execute(
            """SELECT status, previous_status, reason, recent_event_count,
                      previous_event_count, changed_at
               FROM issue_status_history WHERE issue_id=? ORDER BY changed_at DESC""",
            (issue_id,))]
    return detail


def republish(db) -> int:
    """Rebuild the ledger and the exports that are derived from it.

    Both are ``processor``'s own functions run over ``event_records`` and the
    corrections — not an edit by the web process.  The export matters as much
    as the ledger: ``processor.verify`` compares ``issues.jsonl`` against the
    table row for row, so a reassignment that rebuilt one and not the other
    left the integrity check failing until the next pipeline run.  A check that
    reports a problem after every legitimate use of a feature stops being read.
    """
    rebuilt = rebuild_issue_ledger(db)
    export_data(db)
    return rebuilt


class Reassign(BaseModel):
    company_id: str
    from_code: str
    # ``None`` takes the events out of the ledger rather than moving them, which
    # is the honest answer when a code was applied to something that is not an
    # issue at all.
    to_code: str | None = None
    # Omitted means every event currently in the issue — 合并. Naming a subset
    # is 拆分. One primitive, two intents.
    event_ids: list[str] | None = None
    reason: str = Field(min_length=4, max_length=500)


@router.get("/adjustments/log")
def adjustment_log(limit: int = Query(200, ge=1, le=500),
                   session=Depends(current_session),
                   issues=Depends(issue_session)) -> dict[str, Any]:
    """Every correction, including the undone ones.

    An undo writes a timestamp rather than deleting the row: what somebody
    decided and later reversed is the only record of why the ledger looks the
    way it does.
    """
    rows = [dict(row) for row in issue_db.history(issues, limit)]
    return {"items": rows, "active": sum(1 for row in rows if not row["undone_at"]),
            "total": len(rows)}


@router.post("/reassign")
def reassign(body: Reassign, session=Depends(requires("reviewer")),
             db=Depends(db_session), issues=Depends(issue_session)) -> dict[str, Any]:
    """Move events between issue codes, then rebuild the ledger.

    The rebuild is ``processor``'s own function, run over ``event_records`` and
    the corrections — not an edit of the ledger by the web process.  Its output
    is exactly what the next pipeline run will produce, which is the property
    that makes calling it from here safe: a reviewer sees the effect
    immediately and the pipeline does not disagree with them afterwards.
    """
    if body.to_code is not None and body.to_code not in ISSUE_RULES:
        raise HTTPException(status_code=400, detail=f"未知的专题码：{body.to_code}")
    if body.to_code == body.from_code:
        raise HTTPException(status_code=400, detail="源专题码与目标专题码相同")
    adjustments = active_map(db)
    candidates = [row for row in db.execute(
        """SELECT event_id, issue_codes_json FROM event_records
           WHERE company_id=? AND eventhood='EVENT'""", (body.company_id,))
        if belongs(json_field(row["issue_codes_json"]), row["event_id"],
                   body.from_code, adjustments)]
    wanted = set(body.event_ids) if body.event_ids is not None else None
    if wanted is not None:
        # An id that is not in the issue is a mistake worth reporting, not a
        # no-op: it usually means the page and the ledger have drifted apart.
        missing = wanted - {row["event_id"] for row in candidates}
        if missing:
            raise HTTPException(status_code=400,
                                detail=f"有 {len(missing)} 条事件不在该专题下，请刷新后重试")
        candidates = [row for row in candidates if row["event_id"] in wanted]
    if not candidates:
        raise HTTPException(status_code=400, detail="该专题下没有可改派的事件")
    # One transaction. The moves and the ledger they imply land together or not
    # at all — otherwise a rebuild that fails leaves the events moved and the
    # ledger describing the world before the move, with nothing to say so.
    try:
        for row in candidates:
            # Recorded against the code the event actually carries, which after
            # an earlier reassignment is not necessarily the issue it is filed
            # under.
            for code in json_field(row["issue_codes_json"]):
                if adjustments.get((row["event_id"], code), code) != body.from_code:
                    continue
                issue_db.record(issues, company_id=body.company_id, event_id=row["event_id"],
                                from_code=code, to_code=body.to_code, reason=body.reason,
                                actor=session["username"], role=session["role"])
        rebuilt = republish(issues)
        issues.commit()
    except Exception:
        issues.rollback()
        raise
    return {"moved": len(candidates), "issues": rebuilt,
            "from_code": body.from_code, "to_code": body.to_code}


@router.post("/adjustments/{adjustment_id}/undo")
def undo_adjustment(adjustment_id: str, session=Depends(requires("reviewer")),
                    issues=Depends(issue_session)) -> dict[str, Any]:
    try:
        if not issue_db.undo(issues, adjustment_id, session["username"]):
            raise HTTPException(status_code=404, detail="没有这条改派，或它已经被撤销")
        rebuilt = republish(issues)
        issues.commit()
    except Exception:
        issues.rollback()
        raise
    return {"undone": adjustment_id, "issues": rebuilt}
