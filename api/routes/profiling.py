"""How a piece is written, and how it was produced — with the reasoning kept.

Two sidecars have been labelling every event for a while: one judges the
piece's linguistic tone, the other how the piece was produced. Both record, per
row, the label, the model's stated reason, its confidence, the basis the label
rests on, and the model and prompt version that produced it.

The interface showed one word from each and dropped the rest. That is the same
defect those sidecars were written to repair: a value with no stated basis is a
value a reader has to take on faith. So this exposes the columns, and in
particular two distinctions the single word erases:

* **Tone is not impact.** `article_sentiment` describes how a piece is written;
  `impact_direction` describes what the event does. They are output separately
  and allowed to disagree — a dryly-worded penalty filing is neutral in tone and
  negative in impact — and the disagreement is the point rather than an error.
* **A label derived from a graph edge is not a label a model judged.** 14 of the
  production-mode rows carry `basis='propagation_edge'`: the piece was
  identified as a repost because the propagation graph says so, which is a
  stronger claim than a model reading the text. The basis column says which.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.db import has_table, like_clause, total_of
from api.labels import PRODUCTION_MODE_LABELS, PUBLIC_BASES
from api.sources import source_name
from api.routes.events import db_session

router = APIRouter(prefix="/api/profiling", tags=["profiling"])

SENTIMENT_LABELS = {"positive": "正面语气", "neutral": "中性语气", "negative": "负面语气"}
# 键是对外的中性码（I-06）：库里存的是 model，出接口前换成 system，筛选时换回来。
BASIS_LABELS = {"system": "系统研判", "propagation_edge": "由传播关系推定"}
INTERNAL_BASES = {public: internal for internal, public in PUBLIC_BASES.items()}

TABLES = {
    "sentiment": ("article_sentiment_labels", "sentiment", SENTIMENT_LABELS),
    "production": ("production_mode_labels", "mode", PRODUCTION_MODE_LABELS),
}


def available(db, table: str) -> bool:
    return has_table(db, table)


def _list(kind: str, company, value, basis, min_confidence, search, limit, offset, db):
    table, column, labels = TABLES[kind]
    if not available(db, table):
        return {"total": 0, "limit": limit, "offset": offset, "items": [], "available": False}
    filters, parameters = [], []
    for expression, given in ((f"l.{column}=?", value), ("l.company_id=?", company),
                              ("l.basis=?", INTERNAL_BASES.get(basis, basis) if basis else basis)):
        if given:
            filters.append(expression)
            parameters.append(given)
    if min_confidence is not None:
        filters.append("l.confidence >= ?")
        parameters.append(min_confidence)
    if search:
        clause, values = like_clause(("l.reason", "e.summary"), search)
        filters.append(clause)
        parameters.extend(values)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    base = f"""FROM {table} l
               JOIN event_records e ON e.event_id = l.event_id
               LEFT JOIN document_versions v ON v.version_id = e.version_id{where}"""
    total = total_of(db, f"SELECT l.event_id {base}", parameters)
    rows = db.execute(f"""SELECT l.event_id, l.company_id, l.{column} AS value, l.reason,
               l.confidence, l.basis, l.classified_at,
               e.summary, e.event_time, e.impact_direction, e.impact_magnitude,
               v.title, v.source_id, v.canonical_url {base}
        ORDER BY l.confidence DESC, l.event_id LIMIT ? OFFSET ?""",
        [*parameters, limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [{**dict(row), "basis": PUBLIC_BASES.get(row["basis"], row["basis"]),
                       "source_name": source_name(row["source_id"])} for row in rows],
            "labels": {"value": labels, "basis": BASIS_LABELS}}


def _stats(kind: str, company, db):
    table, column, labels = TABLES[kind]
    if not available(db, table):
        return {"available": False, "by_value": [], "by_basis": [], "confidence": [], "totals": {}}
    clause, parameters = (("WHERE l.company_id=?", [company]) if company else ("", []))
    join = f"FROM {table} l {clause}"

    def group(expression: str, extra: str = "") -> list[dict[str, Any]]:
        return [dict(row) for row in db.execute(
            f"SELECT {expression} AS value, count(*) AS count {join} {extra} "
            f"GROUP BY 1 ORDER BY count DESC", parameters)]

    # Confidence in twenty buckets: a model that answers 0.95 to everything and
    # one that spreads across the range are different instruments, and a mean
    # would not tell them apart.
    histogram = [dict(row) for row in db.execute(
        f"""SELECT cast(l.confidence * 20 AS INTEGER) / 20.0 AS bucket, count(*) AS count
            {join} GROUP BY 1 ORDER BY bucket""", parameters)]

    total = db.execute(f"SELECT count(*) {join}", parameters).fetchone()[0]
    return {
        "available": True,
        "totals": {"labelled": total},
        "by_value": [{**row, "label": labels.get(row["value"], row["value"])}
                     for row in group(f"l.{column}")],
        "by_basis": [{**row, "value": PUBLIC_BASES.get(row["value"], row["value"]),
                      "label": BASIS_LABELS.get(PUBLIC_BASES.get(row["value"], row["value"]), row["value"])}
                     for row in group("l.basis")],
        "confidence": histogram,
        # 出自哪个模型、哪一版提示词只留在库里与命令行（I-06）。
    }


@router.get("/sentiment")
def sentiment(company: str | None = None, value: str | None = None, basis: str | None = None,
              min_confidence: float | None = None, search: str | None = None,
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              db=Depends(db_session)) -> dict[str, Any]:
    return _list("sentiment", company, value, basis, min_confidence, search, limit, offset, db)


@router.get("/sentiment/stats")
def sentiment_stats(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    body = _stats("sentiment", company, db)
    if not body["available"]:
        return body
    clause, parameters = (("WHERE l.company_id=?", [company]) if company else ("", []))
    # The cross-tab the field exists for. Tone and impact agreeing on every row
    # would mean one of them is not being judged.
    body["vs_impact"] = [dict(row) for row in db.execute(
        f"""SELECT l.sentiment AS tone, e.impact_direction AS impact, count(*) AS count
            FROM article_sentiment_labels l
            JOIN event_records e ON e.event_id = l.event_id {clause}
            GROUP BY 1, 2 ORDER BY count DESC""", parameters)]
    agree = sum(row["count"] for row in body["vs_impact"] if row["tone"] == row["impact"])
    total = sum(row["count"] for row in body["vs_impact"])
    body["divergence"] = {"agree": agree, "diverge": total - agree,
                          "rate": round((total - agree) / total, 4) if total else None}
    return body


@router.get("/production")
def production(company: str | None = None, value: str | None = None, basis: str | None = None,
               min_confidence: float | None = None, search: str | None = None,
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               db=Depends(db_session)) -> dict[str, Any]:
    return _list("production", company, value, basis, min_confidence, search, limit, offset, db)


@router.get("/production/stats")
def production_stats(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    body = _stats("production", company, db)
    if not body["available"]:
        return body
    clause, parameters = (("WHERE l.company_id=?", [company]) if company else ("", []))
    # Sponsored copy should read positive far more often than a filing does; the
    # cross-tab is the corroboration for both labels at once, neither of which
    # has a gold standard of its own.
    body["vs_sentiment"] = [dict(row) for row in db.execute(
        f"""SELECT l.mode AS mode, s.sentiment AS tone, count(*) AS count
            FROM production_mode_labels l
            JOIN article_sentiment_labels s ON s.event_id = l.event_id {clause}
            GROUP BY 1, 2 ORDER BY mode, count DESC""", parameters)]
    return body
