"""Timeline endpoints — events in order, with the clock and the phase named.

Two things must never be implicit here.  The first is which clock an entry used:
an event time is when something happened, a publish time is when someone wrote
about it.  The second is why a risk score moved: the disclosure phase supplies a
coefficient, and both the base and the adjusted score are returned so the reader
can see the multiplication rather than take it on trust.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.db import json_field, like_clause, paginate, total_of
from api.routes.events import db_session

router = APIRouter(prefix="/api/timeline", tags=["timeline"])

BASE_QUERY = """SELECT t.*, e.summary, e.issue_codes_json, e.impact_direction, e.impact_magnitude,
                       e.needs_review, e.production_mode,
                       v.title, v.source_id, v.source_tier, v.canonical_url, v.published_at
                FROM event_timeline t
                JOIN event_records e ON e.event_id=t.event_id
                JOIN document_versions v ON v.version_id=t.version_id"""


def entry_row(row: Any) -> dict[str, Any]:
    return {
        "event_id": row["event_id"], "company_id": row["company_id"],
        "version_id": row["version_id"], "summary": row["summary"],
        "issue_codes": json_field(row["issue_codes_json"]),
        "timeline_at": row["timeline_at"], "time_basis": row["time_basis"],
        "time_precision": row["time_precision"], "published_at": row["published_at"],
        "phase": row["phase"], "phase_label": row["phase_label"],
        "phase_coefficient": row["phase_coefficient"],
        "base_risk_score": row["base_risk_score"],
        "adjusted_risk_score": row["adjusted_risk_score"],
        "adjusted_risk_level": row["adjusted_risk_level"],
        "claim_count": row["claim_count"], "new_fact_count": row["new_fact_count"],
        "novelty": row["novelty"], "is_new_this_round": bool(row["is_new_this_round"]),
        "impact_direction": row["impact_direction"], "impact_magnitude": row["impact_magnitude"],
        "production_mode": row["production_mode"], "needs_review": bool(row["needs_review"]),
        "title": row["title"], "source_id": row["source_id"], "source_tier": row["source_tier"],
        "canonical_url": row["canonical_url"],
    }


@router.get("")
def list_entries(company: str | None = None, phase: str | None = None, level: str | None = None,
                 issue: str | None = None, basis: str | None = None,
                 new_facts_only: bool = False, search: str | None = None,
                 limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                 db=Depends(db_session)) -> dict[str, Any]:
    filters, parameters = [], []
    for column, value in (("t.company_id", company), ("t.phase", phase),
                          ("t.adjusted_risk_level", level), ("t.time_basis", basis)):
        if value:
            filters.append(f"{column}=?")
            parameters.append(value)
    if issue:
        filters.append("e.issue_codes_json LIKE ?")
        parameters.append(f'%"{issue}"%')
    if new_facts_only:
        filters.append("t.new_fact_count > 0")
    if search:
        clause, values = like_clause(("e.summary", "v.title"), search)
        filters.append(clause)
        parameters.extend(values)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    ordered = f"{BASE_QUERY}{where} ORDER BY t.timeline_at IS NULL, t.timeline_at DESC"
    query, bound = paginate(ordered, parameters, limit, offset)
    return {"total": total_of(db, f"{BASE_QUERY}{where}", parameters),
            "limit": limit, "offset": offset,
            "items": [entry_row(row) for row in db.execute(query, bound).fetchall()]}


@router.get("/phases")
def list_phases(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    """The disclosure phases and what each coefficient is applied to.

    A company with no phase windows is not a gap in the data: an already-listed
    issuer has no offering timeline, so its events sit at a coefficient of 1.0
    and the UI needs to be able to say that rather than show an empty axis.
    """
    clause, parameters = ("WHERE company_id=?", [company]) if company else ("", [])
    phases = db.execute(f"""SELECT * FROM disclosure_phases {clause}
        ORDER BY company_id, ordinal""", parameters).fetchall()
    counts = {(row["company_id"], row["phase"]): {
                  "phase_label": row["phase_label"], "coefficient": row["coefficient"],
                  "events": row["events"], "mean_base": row["mean_base"],
                  "mean_adjusted": row["mean_adjusted"]}
              for row in db.execute(f"""SELECT company_id, phase, phase_label,
                     max(phase_coefficient) AS coefficient, count(*) AS events,
                     round(avg(base_risk_score), 1) AS mean_base,
                     round(avg(adjusted_risk_score), 1) AS mean_adjusted
              FROM event_timeline {clause} GROUP BY company_id, phase""", parameters)}
    items = [{**dict(phase),
              **{key: value for key, value
                 in counts.get((phase["company_id"], phase["phase"]),
                               {"events": 0, "mean_base": None, "mean_adjusted": None}).items()
                 if key not in ("phase_label", "coefficient")}}
             for phase in phases]
    # Events the phase engine could not place — an already-listed issuer has no
    # offering windows at all — are listed as themselves rather than dropped, so
    # the phase counts still add up to the ledger.
    unphased = [{"company_id": key[0], "phase": key[1], "ordinal": None,
                 "started_at": None, "ended_at": None,
                 "rationale": "该主体没有发行阶段窗口，风险分不做阶段修正。", **value}
                for key, value in sorted(counts.items()) if key[1] == "UNKNOWN"]
    return {"items": items + unphased}


@router.get("/rounds")
def list_rounds(db=Depends(db_session)) -> dict[str, Any]:
    """Processing rounds, so "new this round" has a stated reference time."""
    rows = db.execute("SELECT * FROM timeline_rounds ORDER BY ordinal DESC").fetchall()
    # 轮次的内部编号不出接口（I-06）；序号与时间足以说明「本轮」指哪一轮。
    return {"items": [{key: row[key] for key in row.keys() if key != "round_id"}
                      | {"is_baseline": bool(row["is_baseline"])} for row in rows]}
