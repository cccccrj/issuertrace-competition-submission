"""Disclosure version-difference endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.db import like_clause, paginate, total_of
from api.routes.events import db_session

router = APIRouter(prefix="/api/diffs", tags=["diffs"])

BASE_QUERY = "SELECT * FROM disclosure_diffs"


@router.get("")
def list_diffs(company: str | None = None, pair: str | None = None, kind: str | None = None,
               section: str | None = None, search: str | None = None,
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               db=Depends(db_session)) -> dict[str, Any]:
    filters, parameters = [], []
    for column, value in (("company_id", company), ("pair_key", pair),
                          ("diff_kind", kind), ("section", section)):
        if value:
            filters.append(f"{column}=?")
            parameters.append(value)
    if search:
        clause, values = like_clause(("left_text", "right_text"), search)
        filters.append(clause)
        parameters.extend(values)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    # Figure changes lead: a moved paragraph is noise next to a restated number.
    ordered = (f"{BASE_QUERY}{where} ORDER BY CASE diff_kind WHEN 'numeric' THEN 0"
               " WHEN 'modified' THEN 1 ELSE 2 END, section, right_page")
    query, bound = paginate(ordered, parameters, limit, offset)
    return {"total": total_of(db, f"{BASE_QUERY}{where}", parameters),
            "limit": limit, "offset": offset,
            "items": [dict(row) for row in db.execute(query, bound).fetchall()]}


@router.get("/facets")
def diff_facets(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    """The filter values that actually exist, so the UI never offers an empty one."""
    clause, parameters = ("WHERE company_id=?", [company]) if company else ("", [])
    def group(column: str) -> list[dict[str, Any]]:
        return [{"value": row[0], "count": row[1]} for row in db.execute(
            f"SELECT {column}, count(*) FROM disclosure_diffs {clause} GROUP BY 1 ORDER BY 2 DESC",
            parameters)]
    return {"pairs": group("pair_key"), "kinds": group("diff_kind"), "sections": group("section")}
