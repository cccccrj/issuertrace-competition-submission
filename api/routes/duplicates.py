"""Near-duplicate clustering — 命题主要任务第 4 项的证据.

同源事件聚合与去重 is the task the interface served most thinly: the numbers
existed only as a member count on a propagation row, and `duplicate_clusters`
was not exposed at all. 1,464 document versions fold into 297 clusters here, the
largest holding 209 of them, and none of that was visible anywhere.

The distinction this endpoint has to carry, because the two are easy to confuse:
a **near-duplicate cluster** is similarity-based — the same text arriving
through several doors. A **propagation component** is copy-relation-based — a
chain of who republished whom. Ten outlets quoting one prospectus are ten
independent stories in one cluster; ten reposts of one article are one story.
Conflating them is how an independent-source count gets inflated, so the detail
route returns both and the page says which is which.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.db import has_table, like_clause, paginate, total_of
from api.routes.events import db_session

router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])


def clustering_ran(db) -> bool:
    """Whether the clustering step has produced its tables yet.

    A deployment whose pipeline has not reached this stage should see an empty
    page saying so, not a 500. The same rule the health probe follows: degraded
    is a state to report, not an error to raise.
    """
    return has_table(db, "duplicate_clusters")

BASE_QUERY = """SELECT c.cluster_id, c.company_id, c.cluster_kind, c.representative_version_id,
                       c.member_count, c.similarity_threshold, c.created_at,
                       v.title AS representative_title, v.source_id AS representative_source,
                       v.source_tier AS representative_tier, v.published_at, v.canonical_url
                FROM duplicate_clusters c
                LEFT JOIN document_versions v ON v.version_id = c.representative_version_id"""


@router.get("")
def list_clusters(company: str | None = None, min_members: int = Query(2, ge=1),
                  search: str | None = None, cluster: str | None = None,
                  limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0),
                  db=Depends(db_session)) -> dict[str, Any]:
    if not clustering_ran(db):
        return {"total": 0, "limit": limit, "offset": offset, "items": [], "available": False}
    # 指名一个簇就是「让我看这一个」，所以它不再受 member_count 水位管——一条点名
    # 了 cluster_id 的 URL 不该因为第二个参数没跟着调对而落到空列表上。这与传播链路
    # 那边 `version` 的立场相同（那边由前端把 min_members 降到 1）；这里放在服务端，
    # 因为 URL 是本接口对外的承诺，不是调用方的拼装责任。
    filters, parameters = ([], []) if cluster else (["c.member_count >= ?"], [min_members])
    if cluster:
        filters.append("c.cluster_id=?")
        parameters.append(cluster)
    if company:
        filters.append("c.company_id=?")
        parameters.append(company)
    if search:
        clause, values = like_clause(("v.title",), search)
        filters.append(clause)
        parameters.extend(values)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    total = total_of(db, f"SELECT c.cluster_id FROM duplicate_clusters c "
                         f"LEFT JOIN document_versions v ON v.version_id=c.representative_version_id{where}",
                     parameters)
    rows = db.execute(f"{BASE_QUERY}{where} ORDER BY c.member_count DESC, c.cluster_id "
                      f"LIMIT ? OFFSET ?", [*parameters, limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [dict(row) for row in rows]}


@router.get("/stats")
def stats(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    if not clustering_ran(db):
        return {"available": False, "clusters": 0, "members": 0, "collapsed": 0,
                "documents": 0, "size_histogram": [], "largest": None,
                "threshold": {}, "by_company": []}
    clause, parameters = ("WHERE company_id=?", [company]) if company else ("", [])
    sizes = [dict(row) for row in db.execute(
        f"""SELECT member_count AS members, count(*) AS clusters FROM duplicate_clusters
            {clause} GROUP BY 1 ORDER BY members""", parameters)]
    clusters = sum(row["clusters"] for row in sizes)
    members = sum(row["members"] * row["clusters"] for row in sizes)
    largest = db.execute(
        f"""SELECT cluster_id, member_count FROM duplicate_clusters {clause}
            ORDER BY member_count DESC LIMIT 1""", parameters).fetchone()
    documents = db.execute(
        "SELECT count(*) FROM document_versions" + (" WHERE company_id=?" if company else ""),
        parameters).fetchone()[0]
    return {
        "clusters": clusters,
        "members": members,
        # What the clustering actually saved: the versions it folded away.
        "collapsed": members - clusters,
        "documents": documents,
        "size_histogram": sizes,
        "largest": dict(largest) if largest else None,
        "threshold": dict(db.execute(
            f"""SELECT min(similarity_threshold) AS low, max(similarity_threshold) AS high,
                       cluster_kind AS kind FROM duplicate_clusters {clause}""",
            parameters).fetchone() or {}),
        "by_company": [dict(row) for row in db.execute(
            """SELECT company_id, count(*) AS clusters, sum(member_count) AS members
               FROM duplicate_clusters GROUP BY 1 ORDER BY clusters DESC""")],
    }


@router.get("/{cluster_id}")
def cluster(cluster_id: str, db=Depends(db_session)) -> dict[str, Any]:
    if not clustering_ran(db):
        return {"cluster": None, "members": [], "components": [], "available": False}
    row = db.execute(f"{BASE_QUERY} WHERE c.cluster_id=?", (cluster_id,)).fetchone()
    if not row:
        return {"cluster": None, "members": [], "components": []}
    members = [dict(member) for member in db.execute(
        """SELECT m.version_id, v.title, v.source_id, v.source_tier, v.published_at,
                  v.canonical_url, v.document_type
           FROM duplicate_cluster_members m
           LEFT JOIN document_versions v ON v.version_id = m.version_id
           WHERE m.cluster_id=? ORDER BY v.published_at""", (cluster_id,))]
    versions = [member["version_id"] for member in members]
    placeholders = ",".join("?" * len(versions)) or "''"
    # The other kind of grouping, returned beside this one so the page can say
    # what the difference is instead of leaving a reader to assume they are the
    # same thing counted twice.
    components = [dict(component) for component in db.execute(
        f"""SELECT DISTINCT p.component_id, p.member_count, p.source_count
            FROM propagation_component_members m
            JOIN propagation_components p ON p.component_id = m.component_id
            WHERE m.version_id IN ({placeholders})""", versions)] if versions else []
    sources = sorted({member["source_id"] for member in members if member["source_id"]})
    return {
        "cluster": dict(row),
        "members": members,
        "components": components,
        # An independence count that a reader can check against the member list.
        "distinct_sources": len(sources),
        "sources": sources,
    }
