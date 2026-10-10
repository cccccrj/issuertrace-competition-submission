"""One search box for a platform whose data is spread across twenty-eight pages.

Ten pages carry a search box already, and each one searches its own table.  That
works when you know which page holds the thing you want — but which page holds
「募资 295 亿」 is a question about how *this platform* classified it, and a
reader has no reason to know the answer before they look.  Asking them to guess
is asking them to know the taxonomy in order to find their way into it.

So this endpoint asks every table at once and answers in groups.  What it
deliberately does not do is rank across those groups: the six structured groups
are substring matches with no score at all, and disclosure paragraphs come back
with a BM25 score from the FTS index.  Blending the two would require inventing a
common scale, and an invented number that looks like relevance is worse than an
honest ordering — so the group order is fixed and declared, and ranking happens
only inside a group.

The disclosure group is the reason this is worth building.  93,209 indexed
paragraphs of prospectuses, inquiry replies and filings have never had a way in
from any page: ``processor.claim_index.search`` has been able to find them in
milliseconds since claim adjudication was written, and only claim adjudication
ever called it.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.db import has_table, like_clause, total_of
from api.labels import ISSUE_CODE_LABELS
# Every route resolves its connection through this one, so a test that redirects
# it at a fixture redirects the whole app; opening a second one here would quietly
# read the real database instead.
from api.routes.events import db_session
from processor.claim_index import search as evidence_search
from processor.pipeline import configured_companies

router = APIRouter(prefix="/api/search", tags=["search"])

# Groups are answered in this order, and the page renders them in it.  It is a
# statement about what a reader most often means, not a relevance ranking.
# 组名说的是**找到的是什么东西**，用词与它通往的那一页一致——检索页恰恰是最不能
# 自己另起一套说法的地方：一个读者在这里看到「议题」、点进去却是「专题跟踪」，
# 他学到的是这套系统有两个词表，而不是一个。
GROUP_LABELS = {
    "event": "事件",            # 侧栏「事件库」，单条就是事件
    "claim": "核验点",          # 风险事项标题里就写「（3 条核验点）」
    "issue": "专题",            # 侧栏「专题跟踪」，简报里「重点专题」
    "alert": "风险事项",        # 业务化重组之后全站的叫法，「预警」只剩下推送那一侧
    "diff": "披露变化",         # 风险简报第七节就叫「七、披露变化」
    "document": "原始文档",     # 「文档」在一个到处都是文档的平台上等于没说
    "evidence": "披露原文",     # 本来就是大白话，不动
}


def group(kind: str, items: list[dict[str, Any]], total: int | None,
          more: dict[str, Any] | None = None) -> dict[str, Any]:
    """One answer group.

    ``total`` is None where counting would cost a second full scan for a number
    nobody can act on; the page then says "前 N 条" rather than showing a total
    it does not have.
    """
    return {"kind": kind, "label": GROUP_LABELS[kind], "total": total,
            "items": items, "more": more}


def events(db: Any, term: str, limit: int, company: str | None) -> dict[str, Any]:
    clause, values = like_clause(("e.summary", "v.title"), term)
    base = ("""SELECT e.event_id, e.company_id, e.summary, e.event_time, e.risk_level,
                      v.title, v.published_at
               FROM event_records e JOIN document_versions v ON v.version_id=e.version_id
               WHERE """ + clause)
    parameters = list(values)
    if company:
        base += " AND e.company_id=?"
        parameters.append(company)
    rows = db.execute(f"{base} ORDER BY COALESCE(e.event_time, v.published_at) DESC LIMIT ?",
                      [*parameters, limit]).fetchall()
    return group("event", [dict(row) for row in rows], total_of(db, base, parameters),
                 {"path": "/events", "query": {"search": term}})


def claims(db: Any, term: str, limit: int, company: str | None) -> dict[str, Any]:
    clause, values = like_clause(("c.text",), term)
    base = ("""SELECT c.claim_id, c.event_id, c.text, c.issue_code, e.company_id,
                      v.verdict, v.partial_subtype
               FROM event_claims c
               JOIN event_records e ON e.event_id=c.event_id
               LEFT JOIN claim_verdicts v ON v.claim_id=c.claim_id
               WHERE """ + clause)
    parameters = list(values)
    if company:
        base += " AND e.company_id=?"
        parameters.append(company)
    rows = db.execute(f"{base} LIMIT ?", [*parameters, limit]).fetchall()
    return group("claim", [dict(row) for row in rows], total_of(db, base, parameters),
                 {"path": "/claims", "query": {"search": term}})


def issues(db: Any, term: str, limit: int, company: str | None) -> dict[str, Any]:
    """Issues match on the Chinese label as well as the code.

    Nobody types SUPPLY_CHAIN.  The ledger stores only the code, and the label
    map the pages render from is `api/labels.py`, so the match happens against
    both and the 42 rows make a full scan the cheapest way to do it.
    """
    folded = term.strip().lower()
    matched = [code for code, label in ISSUE_CODE_LABELS.items()
               if folded in code.lower() or term.strip() in label]
    if not matched:
        return group("issue", [], 0)
    placeholders = ",".join("?" * len(matched))
    base = (f"""SELECT issue_id, company_id, issue_code, event_count, independent_source_count,
                       risk_level, status FROM issue_ledger WHERE issue_code IN ({placeholders})""")
    parameters = list(matched)
    if company:
        base += " AND company_id=?"
        parameters.append(company)
    rows = db.execute(f"{base} ORDER BY event_count DESC LIMIT ?",
                      [*parameters, limit]).fetchall()
    items = [{**dict(row), "issue_label": ISSUE_CODE_LABELS.get(row["issue_code"])}
             for row in rows]
    return group("issue", items, total_of(db, base, parameters))


def alerts(db: Any, term: str, limit: int, company: str | None) -> dict[str, Any]:
    clause, values = like_clause(("a.headline", "a.summary"), term)
    base = ("""SELECT a.alert_id, a.company_id, a.headline, a.summary, a.risk_level,
                      a.adjusted_risk_score, a.issue_code
               FROM alerts a WHERE """ + clause)
    parameters = list(values)
    if company:
        base += " AND a.company_id=?"
        parameters.append(company)
    rows = db.execute(f"{base} ORDER BY a.adjusted_risk_score DESC LIMIT ?",
                      [*parameters, limit]).fetchall()
    return group("alert", [dict(row) for row in rows], total_of(db, base, parameters),
                 # /alerts 在业务化重组时退役了，这条「查看全部」此前指向一个不存在的
                 # 路由——检索页是唯一还在用它的地方，改名这一趟才把它照出来。
                 {"path": "/risks", "query": {"search": term}})


def documents(db: Any, term: str, limit: int, company: str | None) -> dict[str, Any]:
    clause, values = like_clause(("v.title",), term)
    base = ("""SELECT v.version_id, v.company_id, v.title, v.source_id, v.document_type,
                      v.published_at FROM document_versions v WHERE """ + clause)
    parameters = list(values)
    if company:
        base += " AND v.company_id=?"
        parameters.append(company)
    rows = db.execute(f"{base} ORDER BY v.published_at DESC LIMIT ?",
                      [*parameters, limit]).fetchall()
    return group("document", [dict(row) for row in rows], total_of(db, base, parameters),
                 {"path": "/ingestion", "query": {"search": term}})


def evidence(db: Any, term: str, limit: int, company: str | None) -> dict[str, Any]:
    """Paragraphs of the filings themselves, through the index that already exists.

    ``claim_index.search`` takes one company because a claim always belongs to
    one; a search box does not, so it asks each configured company and merges on
    the score.  BM25 values come from a single shared index, so they are
    comparable across that loop — which is exactly why no attempt is made to
    compare them with the substring groups above.

    No total: counting FTS matches means a second pass over the posting lists
    for a number the reader cannot page through anyway.
    """
    if not has_table(db, "evidence_fts"):
        return group("evidence", [], None)
    targets = [company] if company else list(configured_companies())
    found: list[dict[str, Any]] = []
    for company_id in targets:
        for row in evidence_search(db, company_id, term, limit):
            found.append({**row, "company_id": company_id})
    found.sort(key=lambda row: row["score"])
    return group("evidence", found[:limit], None)


def diffs(db: Any, term: str, limit: int, company: str | None) -> dict[str, Any]:
    """Version-to-version changes in the filings, which no other group covers.

    An event is what was reported and a document is what was filed; a diff is
    the sentence that reads differently in the next draft, and searching for it
    is how someone checks whether a number moved.
    """
    if not has_table(db, "disclosure_diffs"):
        return group("diff", [], 0)
    clause, values = like_clause(("left_text", "right_text"), term)
    base = ("""SELECT diff_id, company_id, section, diff_kind, metric, old_value, new_value,
                      left_text, right_text
               FROM disclosure_diffs WHERE """ + clause)
    parameters = list(values)
    if company:
        base += " AND company_id=?"
        parameters.append(company)
    rows = db.execute(f"{base} LIMIT ?", [*parameters, limit]).fetchall()
    return group("diff", [dict(row) for row in rows], total_of(db, base, parameters),
                 {"path": "/diffs", "query": {"search": term}})


@router.get("")
def search(q: str = Query(..., min_length=1), limit: int = Query(5, ge=1, le=20),
           company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    term = q.strip()
    # A box holding only spaces is not an error, it is an empty box; answering
    # 400 would put a red message under a search nobody has typed yet.
    if not term:
        return {"query": "", "limit": limit, "groups": []}
    return {"query": term, "limit": limit,
            "groups": [events(db, term, limit, company),
                       claims(db, term, limit, company),
                       issues(db, term, limit, company),
                       alerts(db, term, limit, company),
                       diffs(db, term, limit, company),
                       documents(db, term, limit, company),
                       evidence(db, term, limit, company)]}
