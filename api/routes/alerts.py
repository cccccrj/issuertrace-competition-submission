"""Alert endpoints — the events a person is asked to look at, and why.

An alert is only useful if it can be argued with, so every row carries the named
rules that raised it, the phase coefficient that scaled its risk, and the
components of its credibility score.  None of that is computed here; ``processor
.alerts`` already decided it.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db import json_field, like_clause, paginate, total_of
from api.routes.events import db_session
from api.labels import METHOD_LABELS, public_method
from api.sources import source_name

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

BASE_QUERY = """SELECT a.*, v.title AS lead_title, v.source_id AS lead_source,
                       v.source_tier AS lead_source_tier, v.canonical_url AS lead_url
                FROM alerts a JOIN document_versions v ON v.version_id=a.lead_version_id"""


def public_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """核验点快照的对外形状：研判方式换成中性码与标签（I-06）。"""
    for item in evidence:
        item["method"] = public_method(item.get("method"))
        item["method_label"] = METHOD_LABELS.get(item["method"])
    return evidence


def alert_row(row: Any) -> dict[str, Any]:
    return {
        "alert_id": row["alert_id"], "company_id": row["company_id"],
        "issue_code": row["issue_code"], "issue_id": row["issue_id"],
        "issue_status": row["issue_status"], "status": row["status"],
        "headline": row["headline"], "summary": row["summary"],
        "window_start": row["window_start"], "window_end": row["window_end"],
        "subjects": json_field(row["subjects_json"]),
        "affected_objects": json_field(row["affected_objects_json"]),
        "credibility": row["credibility"], "credibility_label": row["credibility_label"],
        # The workings travel with the number: a reader who rejects the source
        # tier can discount the score without discarding the whole alert.
        "credibility_basis": json_field(row["credibility_basis_json"], {}),
        "risk_score": row["risk_score"], "adjusted_risk_score": row["adjusted_risk_score"],
        "risk_level": row["risk_level"],
        "phase": row["phase"], "phase_label": row["phase_label"],
        "phase_coefficient": row["phase_coefficient"],
        "reasons": json_field(row["reasons_json"]),
        "event_count": row["event_count"], "new_this_round": bool(row["new_this_round"]),
        "updated_at": row["updated_at"],
        "lead_event_id": row["lead_event_id"], "lead_version_id": row["lead_version_id"],
        "lead_title": row["lead_title"], "lead_source": row["lead_source"],
        "lead_source_name": source_name(row["lead_source"]),
        "lead_source_tier": row["lead_source_tier"], "lead_url": row["lead_url"],
    }


def filter_clause(company: str | None, level: str | None, credibility: str | None,
                  issue: str | None, phase: str | None, reason: str | None,
                  status: str | None, search: str | None) -> tuple[str, list[Any]]:
    filters, parameters = [], []
    for column, value in (("a.company_id", company), ("a.risk_level", level),
                          ("a.credibility_label", credibility), ("a.issue_code", issue),
                          ("a.phase", phase), ("a.status", status)):
        if value:
            filters.append(f"{column}=?")
            parameters.append(value)
    if reason:
        # reasons_json is an array of {code,label}; matching the quoted code keeps
        # one rule from selecting another whose code merely starts the same way.
        filters.append("a.reasons_json LIKE ?")
        parameters.append(f'%"code": "{reason}"%')
    if search:
        clause, values = like_clause(("a.headline", "a.summary"), search)
        filters.append(clause)
        parameters.extend(values)
    return (f" WHERE {' AND '.join(filters)}" if filters else "", parameters)


@router.get("")
def list_alerts(company: str | None = None, level: str | None = None,
                credibility: str | None = None, issue: str | None = None,
                phase: str | None = None, reason: str | None = None,
                status: str | None = None, search: str | None = None,
                limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0),
                db=Depends(db_session)) -> dict[str, Any]:
    where, parameters = filter_clause(company, level, credibility, issue, phase, reason,
                                      status, search)
    # Adjusted risk leads, then recency: an old high-risk story still outranks a
    # fresh routine one, which is the whole point of scoring it.
    ordered = f"{BASE_QUERY}{where} ORDER BY a.adjusted_risk_score DESC, a.window_end DESC"
    query, bound = paginate(ordered, parameters, limit, offset)
    return {"total": total_of(db, f"{BASE_QUERY}{where}", parameters),
            "limit": limit, "offset": offset,
            "items": [alert_row(row) for row in db.execute(query, bound).fetchall()]}


@router.get("/facets")
def alert_facets(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    """Only the filter values that exist, plus the rule mix behind them."""
    clause, parameters = ("WHERE company_id=?", [company]) if company else ("", [])

    def group(column: str) -> list[dict[str, Any]]:
        return [{"value": row[0], "count": row[1]} for row in db.execute(
            f"SELECT {column}, count(*) FROM alerts {clause} GROUP BY 1 ORDER BY 2 DESC", parameters)
            if row[0] is not None]

    # One alert can be raised by several rules, so reason counts are tallied over
    # the stored arrays rather than by grouping rows.
    reasons: dict[str, dict[str, Any]] = {}
    for (payload,) in db.execute(f"SELECT reasons_json FROM alerts {clause}", parameters):
        for entry in json_field(payload):
            found = reasons.setdefault(entry["code"], {"value": entry["code"],
                                                       "label": entry.get("label", entry["code"]),
                                                       "count": 0})
            found["count"] += 1
    return {"levels": group("risk_level"), "credibility": group("credibility_label"),
            "issues": group("issue_code"), "phases": group("phase"),
            "reasons": sorted(reasons.values(), key=lambda row: -row["count"])}


@router.get("/{alert_id}")
def get_alert(alert_id: str, db=Depends(db_session)) -> dict[str, Any]:
    row = db.execute(f"{BASE_QUERY} WHERE a.alert_id=?", (alert_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="alert not found")
    detail = alert_row(row)
    # evidence 是 processor/alerts.py 写进 evidence_json 的快照，里面没有 URL。
    # 不改处理层——那要重跑全量 alerts，而这只是一个展示用的字段；在读取时按
    # version_id 补一次。快照本身仍然是当时那一版，补的只是「去哪儿看原件」。
    evidence = json_field(row["evidence_json"]) or []
    version_ids = [item.get("evidence_version_id") for item in evidence
                   if item.get("evidence_version_id")]
    if version_ids:
        placeholders = ",".join("?" for _ in version_ids)
        urls = {r["version_id"]: r["canonical_url"] for r in db.execute(
            f"SELECT version_id, canonical_url FROM document_versions "
            f"WHERE version_id IN ({placeholders})", version_ids)}
        for item in evidence:
            item["evidence_url"] = urls.get(item.get("evidence_version_id"))
    detail["evidence"] = public_evidence(evidence)
    detail["timeline"] = json_field(row["timeline_json"], {})
    event_ids = json_field(row["event_ids_json"])
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        events = db.execute(f"""SELECT e.event_id, e.summary, e.impact_direction, e.risk_score,
                                       t.timeline_at, t.time_basis, t.adjusted_risk_score,
                                       t.adjusted_risk_level, t.new_fact_count, t.claim_count,
                                       v.title, v.source_id, v.source_tier, v.canonical_url
            FROM event_records e
            LEFT JOIN event_timeline t ON t.event_id=e.event_id
            JOIN document_versions v ON v.version_id=e.version_id
            WHERE e.event_id IN ({placeholders})
            ORDER BY t.timeline_at""", event_ids).fetchall()
        detail["events"] = [{**dict(event), "source_name": source_name(event["source_id"])} for event in events]
    else:
        detail["events"] = []
    return detail
