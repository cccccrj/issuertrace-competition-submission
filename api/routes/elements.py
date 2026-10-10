"""Normalised event elements — 命题主要任务第 2 项的证据.

要素抽取与标准化 has two halves. The extraction half is visible on every event
detail page; the standardisation half was not visible anywhere. `event_elements`
holds 31,383 rows recording, per element, the raw text that was found, what it
normalised to, in what unit, on what basis, and whether it resolved at all --
and the API only ever read the flattened JSON arrays on `event_records`, which
keep the values and drop every one of those columns.

The figure the page exists to show is the resolution rate, and it is not
flattering everywhere: amounts resolve at 94%, while subjects resolve at 41%.
Publishing the second number is the point. A standardisation step that reports
only its successes is not a measurement.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.db import has_table, json_field, like_clause, total_of
from api.routes.events import db_session

router = APIRouter(prefix="/api/elements", tags=["elements"])

#: The vocabulary as the extractor writes it, in the order a reader thinks about
#: an event: who, when, how much, and then the rest.
ELEMENT_TYPE_LABELS = {
    "subject": "事件主体", "time": "时间", "amount": "金额", "ratio": "比例",
    "person": "人物", "organization": "机构", "product": "产品",
}
STATUS_LABELS = {"normalized": "已标准化", "unresolved": "未解析"}


def available(db) -> bool:
    return has_table(db, "event_elements")


@router.get("")
def list_elements(company: str | None = None, event: str | None = None,
                  element_type: str | None = None, status: str | None = None,
                  unit: str | None = None, search: str | None = None,
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  db=Depends(db_session)) -> dict[str, Any]:
    if not available(db):
        return {"total": 0, "limit": limit, "offset": offset, "items": [], "available": False}
    filters, parameters = [], []
    for column, value in (("e.company_id", company), ("x.event_id", event),
                          ("x.element_type", element_type), ("x.status", status),
                          ("x.unit", unit)):
        if value:
            filters.append(f"{column}=?")
            parameters.append(value)
    if search:
        clause, values = like_clause(("x.raw_text", "x.normalized_value"), search)
        filters.append(clause)
        parameters.extend(values)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    base = f"""FROM event_elements x
               JOIN event_records e ON e.event_id = x.event_id
               LEFT JOIN document_versions v ON v.version_id = e.version_id{where}"""
    total = total_of(db, f"SELECT x.element_id {base}", parameters)
    rows = db.execute(f"""SELECT x.element_id, x.event_id, x.element_type, x.ordinal,
               x.raw_text, x.normalized_value, x.unit, x.basis, x.status, x.extra_json,
               e.company_id, e.summary AS event_summary, e.event_time,
               v.title, v.source_id {base}
        ORDER BY x.event_id, x.ordinal LIMIT ? OFFSET ?""",
        [*parameters, limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [{**dict(row), "extra": json_field(row["extra_json"])} for row in rows],
            "labels": {"element_type": ELEMENT_TYPE_LABELS, "status": STATUS_LABELS}}


@router.get("/stats")
def stats(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    if not available(db):
        return {"available": False, "totals": {}, "by_type": [], "by_unit": [], "by_basis": []}
    # 不筛公司时不 JOIN：这一页四条聚合各扫一遍 31,137 行要素，白挂一个 JOIN 让每条从
    # 58ms 变成 98ms。JOIN 存在的唯一理由是按公司过滤。
    if company:
        clause, parameters = "WHERE e.company_id=?", [company]
        join = f"""FROM event_elements x
               JOIN event_records e ON e.event_id = x.event_id {clause}"""
    else:
        clause, parameters = "", []
        join = "FROM event_elements x"
    grid = db.execute(f"""SELECT x.element_type, x.status, count(*) AS count {join}
        GROUP BY 1, 2""", parameters).fetchall()

    per_type: dict[str, dict[str, int]] = {}
    for row in grid:
        per_type.setdefault(row["element_type"], {})[row["status"]] = row["count"]

    by_type = []
    for element_type, counts in per_type.items():
        normalized = counts.get("normalized", 0)
        total = sum(counts.values())
        by_type.append({
            "element_type": element_type,
            "label": ELEMENT_TYPE_LABELS.get(element_type, element_type),
            "total": total,
            "normalized": normalized,
            "unresolved": total - normalized,
            # Reported per type rather than only in aggregate: one overall rate
            # would hide that subjects resolve at less than half the rate
            # amounts do, which is the finding worth acting on.
            "normalized_rate": round(normalized / total, 4) if total else None,
        })
    by_type.sort(key=lambda row: -row["total"])

    elements = sum(row["total"] for row in by_type)
    normalized = sum(row["normalized"] for row in by_type)
    return {
        "available": True,
        "totals": {
            "elements": elements,
            "events": db.execute(f"SELECT count(DISTINCT x.event_id) {join}",
                                 parameters).fetchone()[0],
            "normalized": normalized,
            "normalized_rate": round(normalized / elements, 4) if elements else None,
        },
        "by_type": by_type,
        "by_unit": [dict(row) for row in db.execute(
            f"""SELECT x.unit AS unit, count(*) AS count {join}
                {'AND' if clause else 'WHERE'} x.unit IS NOT NULL
                GROUP BY 1 ORDER BY count DESC LIMIT 20""", parameters)],
        # `basis` records how a value was arrived at -- read off the text, or
        # computed relative to the publication date. A normalised time whose
        # basis is relative is a weaker fact than one that was stated, and the
        # difference belongs on screen rather than in the column.
        "by_basis": [dict(row) for row in db.execute(
            f"""SELECT coalesce(x.basis, '未记录') AS basis, count(*) AS count {join}
                GROUP BY 1 ORDER BY count DESC""", parameters)],
    }
