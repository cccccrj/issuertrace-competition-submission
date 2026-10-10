"""Propagation endpoints — how one story spread, and how far it really did.

A component groups versions linked by copying only.  That distinction is the
point of the endpoint: ten articles that all quote the same prospectus are ten
independent stories, while ten reposts of one article are a single one wearing
ten bylines, and the corroboration count must not confuse the two.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db import like_clause, paginate, total_of
from api.routes.events import db_session

router = APIRouter(prefix="/api/propagation", tags=["propagation"])

BASE_QUERY = """SELECT c.*, v.title AS root_title, v.source_id AS root_source,
                       v.source_tier AS root_source_tier, v.document_type AS root_document_type,
                       v.canonical_url AS root_url
                FROM propagation_components c
                JOIN document_versions v ON v.version_id=c.root_version_id"""


@router.get("")
def list_components(company: str | None = None, version: str | None = None,
                    min_members: int = Query(2, ge=1), search: str | None = None,
                    limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0),
                    db=Depends(db_session)) -> dict[str, Any]:
    filters, parameters = ["c.member_count >= ?"], [min_members]
    if company:
        filters.append("c.company_id=?")
        parameters.append(company)
    if version:
        # Answering "which chain is this document part of" without making the
        # caller fetch every component first.
        filters.append("""c.component_id IN (SELECT component_id
            FROM propagation_component_members WHERE version_id=?)""")
        parameters.append(version)
    if search:
        clause, values = like_clause(("v.title",), search)
        filters.append(clause)
        parameters.extend(values)
    where = f" WHERE {' AND '.join(filters)}"
    ordered = (f"{BASE_QUERY}{where} ORDER BY c.member_count DESC, c.source_count DESC,"
               " c.last_published_at DESC")
    query, bound = paginate(ordered, parameters, limit, offset)
    return {"total": total_of(db, f"{BASE_QUERY}{where}", parameters),
            "limit": limit, "offset": offset,
            "items": [dict(row) for row in db.execute(query, bound).fetchall()]}


@router.get("/stats")
def propagation_stats(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    """Edge mix and chain sizes — the corpus-level shape of the propagation graph."""
    clause, parameters = ("WHERE company_id=?", [company]) if company else ("", [])
    kinds = [{"value": row[0], "count": row[1]} for row in db.execute(
        f"SELECT edge_kind, count(*) FROM propagation_edges {clause} GROUP BY 1 ORDER BY 2 DESC",
        parameters)]
    origins = [{"value": row[0], "count": row[1]} for row in db.execute(
        f"SELECT from_kind, count(*) FROM propagation_edges {clause} GROUP BY 1 ORDER BY 2 DESC",
        parameters)]
    totals = db.execute(f"""SELECT count(*) AS components,
               sum(CASE WHEN member_count > 1 THEN 1 ELSE 0 END) AS multi_member,
               sum(CASE WHEN source_count > 1 THEN 1 ELSE 0 END) AS cross_source,
               max(member_count) AS largest
        FROM propagation_components {clause}""", parameters).fetchone()
    return {"edge_kinds": kinds, "origins": origins, **dict(totals)}


@router.get("/{component_id}")
def get_component(component_id: str, db=Depends(db_session)) -> dict[str, Any]:
    component = db.execute(f"{BASE_QUERY} WHERE c.component_id=?", (component_id,)).fetchone()
    if not component:
        raise HTTPException(status_code=404, detail="component not found")
    rows = db.execute("""SELECT m.version_id, m.depth, v.title, v.source_id, v.source_tier,
                                v.document_type, v.published_at, v.canonical_url
        FROM propagation_component_members m
        JOIN document_versions v ON v.version_id=m.version_id
        WHERE m.component_id=? ORDER BY m.depth, v.published_at""", (component_id,)).fetchall()
    version_ids = [row["version_id"] for row in rows]
    # One version can carry several events, so events are attached rather than
    # joined: a join would silently repeat the version and inflate the chain.
    events: dict[str, list[dict[str, Any]]] = {}
    if version_ids:
        placeholders = ",".join("?" for _ in version_ids)
        for event in db.execute(f"""SELECT event_id, version_id, company_id, summary, risk_level
                FROM event_records WHERE version_id IN ({placeholders})""", version_ids):
            events.setdefault(event["version_id"], []).append(dict(event))
    members = [{**dict(row), "events": events.get(row["version_id"], [])} for row in rows]
    # Every edge touching the chain, including the 源于披露 and 回应 links that were
    # deliberately kept out of the grouping: they explain the chain without
    # being allowed to merge it with anything else.
    edges = [] if not version_ids else db.execute(
        f"""SELECT g.*, a.title AS from_title, a.source_id AS from_source,
                   b.title AS to_title, b.source_id AS to_source
            FROM propagation_edges g
            JOIN document_versions a ON a.version_id=g.from_version_id
            JOIN document_versions b ON b.version_id=g.to_version_id
            WHERE g.to_version_id IN ({placeholders}) OR g.from_version_id IN ({placeholders})
            ORDER BY g.lag_hours IS NULL, g.lag_hours""",
        [*version_ids, *version_ids]).fetchall()
    return {**dict(component), "members": members, "edges": [dict(edge) for edge in edges]}
