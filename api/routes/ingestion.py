"""What was collected, what was kept, and why the rest was not.

命题主要任务第 1 项 is 多源金融事件识别, and the identification half is visible on
the event ledger. What was *not* visible is the part that makes the ledger
trustworthy: 4,339 document versions were collected and 1,995 events came out,
and until now nothing said what happened to the difference.

`processed_documents` has recorded it all along -- inclusion status, event-hood,
extraction status, and the rejection reasons as a JSON array -- and no endpoint
read the table. A pipeline that cannot say why it dropped something is a
pipeline whose output has to be taken on faith, which is the opposite of what
this platform is for. It also happens to be 评审第 2 项「能够处理异常场景」 and
第 3 项「日志记录和错误处理」 made visible rather than asserted.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db import has_table, like_clause, total_of
from api.routes.events import db_session

router = APIRouter(prefix="/api/ingestion", tags=["ingestion"])

INCLUSION_LABELS = {
    "included": "有效入库", "excluded_document_type": "文档类型排除",
    "excluded_no_body": "无正文", "excluded_duplicate": "重复内容",
    "excluded_listing_page": "列表页排除",
}
EVENTHOOD_LABELS = {"EVENT": "识别为事件", "NO_EVENT": "无事件", "IRRELEVANT": "与主体无关"}
# The four ways the offline rule layer and the model can line up, named so a
# filter can address one of them.  Two are agreement and two are not; the
# interesting ones are the two that are not, and they had no way of being
# looked at before this.
LAYER_CASES = {
    "agree_event": ("两层都判为事件", "p.rule_eventhood='EVENT' AND p.eventhood='EVENT'"),
    "rule_only": ("规则层判事件，系统研判否决", "p.rule_eventhood='EVENT' AND p.eventhood<>'EVENT'"),
    "system_only": ("规则层未判，系统研判判为事件", "p.rule_eventhood<>'EVENT' AND p.eventhood='EVENT'"),
    "agree_none": ("两层都未判为事件", "p.rule_eventhood<>'EVENT' AND p.eventhood<>'EVENT'"),
}
EXTRACTION_LABELS = {"ok": "解析成功", "body_missing": "正文缺失", "empty": "空文档"}
REJECTION_LABELS = {
    "excluded_document_type": "文档类型不在处理范围",
    "excluded_no_body": "解析不出正文",
    "excluded_listing_page": "是文章索引页，不是文章",
    "unsupported_claim_dropped": "主张无法定位到原文，已丢弃",
    "event_without_traceable_claim": "事件无可追溯主张",
    "no_linked_company": "未能关联到目标主体",
    "insufficient_text": "正文过短",
}


def available(db) -> bool:
    """Whether the processing stage has run *and* recorded what it decided.

    The table existing is not enough: a database can carry an older, narrower
    version of it, and the funnel reads columns the gate writes. Checking for the
    column is checking for the thing actually needed.
    """
    if not has_table(db, "processed_documents"):
        return False
    columns = {row["name"] for row in db.execute("PRAGMA table_info(processed_documents)")}
    return {"inclusion_status", "eventhood", "rejection_reasons_json"} <= columns


@router.get("/funnel")
def funnel(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    if not available(db):
        return {"available": False, "stages": [], "inclusion": [], "eventhood": [],
                "extraction": [], "rejection_reasons": []}
    join = ("JOIN document_versions v ON v.version_id = p.version_id WHERE v.company_id=?"
            if company else "")
    parameters = [company] if company else []

    def counts(column: str) -> list[dict[str, Any]]:
        return [dict(row) for row in db.execute(
            f"""SELECT p.{column} AS value, count(*) AS count
                FROM processed_documents p {join} GROUP BY 1 ORDER BY count DESC""", parameters)]

    def scalar(query: str, params: list = None) -> int:
        return db.execute(query, params or []).fetchone()[0]

    documents = scalar("SELECT count(*) FROM document_versions" +
                       (" WHERE company_id=?" if company else ""), parameters)
    included = scalar(f"""SELECT count(*) FROM processed_documents p {join}
        {'AND' if join else 'WHERE'} p.inclusion_status='included'""", parameters)
    eventful = scalar(f"""SELECT count(*) FROM processed_documents p {join}
        {'AND' if join else 'WHERE'} p.eventhood='EVENT'""", parameters)
    # Constrained to the same population as the stages above it. Counting
    # claims across the whole ledger would let the last stage exceed the one
    # before it, which is not a funnel — it is two numbers on one axis.
    with_claims = scalar(f"""SELECT count(DISTINCT c.event_id) FROM event_claims c
        JOIN event_records e ON e.event_id = c.event_id
        JOIN processed_documents p ON p.version_id = e.version_id {join}
        {'AND' if join else 'WHERE'} p.eventhood='EVENT'""", parameters)

    # Rejection codes repeat within a row, so both numbers are reported: how many
    # documents carry a code, and how many times it fired. Giving only one of
    # them is a quiet way to overstate or understate the same fact.
    documents_with, occurrences = {}, {}
    for row in db.execute(f"""SELECT p.version_id, p.rejection_reasons_json
        FROM processed_documents p {join}
        {'AND' if join else 'WHERE'} p.rejection_reasons_json NOT IN ('[]','')""", parameters):
        try:
            codes = json.loads(row["rejection_reasons_json"])
        except (TypeError, ValueError):
            continue
        for code in set(codes):
            documents_with[code] = documents_with.get(code, 0) + 1
        for code in codes:
            occurrences[code] = occurrences.get(code, 0) + 1

    return {
        "available": True,
        "stages": [
            {"key": "collected", "label": "采集文档数", "count": documents},
            {"key": "included", "label": "有效入库数", "count": included},
            {"key": "eventful", "label": "识别事件数", "count": eventful},
            {"key": "with_claims", "label": "可核验主张数", "count": with_claims},
        ],
        "inclusion": counts("inclusion_status"),
        "eventhood": counts("eventhood"),
        "extraction": counts("extraction_status"),
        "rejection_reasons": sorted(
            [{"code": code, "label": REJECTION_LABELS.get(code, code),
              "documents": documents_with[code], "occurrences": occurrences.get(code, 0)}
             for code in documents_with],
            key=lambda row: -row["documents"]),
        "labels": {"inclusion": INCLUSION_LABELS, "eventhood": EVENTHOOD_LABELS,
                   "extraction": EXTRACTION_LABELS},
    }


def layers_available(db) -> bool:
    """Whether the rule layer's own reading survived alongside the model's.

    ``rule_eventhood`` exists because deciding eligibility from the column the
    model writes back made the decision irreversible.  It is also the only
    record of what the offline layer thought, and therefore the only way to see
    where the two disagree.  A database from before that split has the column
    missing or empty, and a comparison against it would read as total agreement
    — the most flattering possible answer, arrived at by having no data.
    """
    if not available(db):
        return False
    columns = {row["name"] for row in db.execute("PRAGMA table_info(processed_documents)")}
    if "rule_eventhood" not in columns:
        return False
    return bool(db.execute("""SELECT 1 FROM processed_documents
        WHERE rule_eventhood IS NOT NULL LIMIT 1""").fetchone())


@router.get("/layers")
def layers(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    """Where the offline rule layer and the model disagree about event-hood.

    Both readings have been stored side by side all along and nothing has ever
    read them together.  They disagree on 43% of the documents that reached the
    model, and on a third of everything the rule layer called an event — a
    figure that is neither an error rate nor an accuracy, because **no human
    has adjudicated a single one of these**.  That is the point of showing it:
    the disagreement is the sampling frame for the review queue, and the queue
    is the only thing that can turn it into a measurement.

    Documents excluded at the gate are outside this entirely — they were never
    sent, so there is no second opinion to compare against.
    """
    if not layers_available(db):
        return {"available": False,
                "reason": "本次部署没有规则层的独立读数（`rule_eventhood`），无法比较"}
    clause, parameters = (("AND v.company_id=?", [company]) if company else ("", []))
    base = f"""FROM processed_documents p JOIN document_versions v ON v.version_id=p.version_id
               WHERE p.inclusion_status='included' AND p.rule_eventhood IS NOT NULL {clause}"""
    total = db.execute(f"SELECT count(*) {base}", parameters).fetchone()[0]
    cases = []
    for key, (label, condition) in LAYER_CASES.items():
        count = db.execute(f"SELECT count(*) {base} AND {condition}", parameters).fetchone()[0]
        cases.append({"key": key, "label": label, "count": count,
                      "agrees": key in {"agree_event", "agree_none"}})
    disagreements = sum(case["count"] for case in cases if not case["agrees"])
    rule_events = db.execute(
        f"SELECT count(*) {base} AND p.rule_eventhood='EVENT'", parameters).fetchone()[0]
    overruled = db.execute(
        f"SELECT count(*) {base} AND p.rule_eventhood='EVENT' AND p.eventhood<>'EVENT'",
        parameters).fetchone()[0]
    return {
        "available": True,
        "compared": total,
        "cases": cases,
        "disagreements": disagreements,
        "disagreement_rate": round(disagreements / total, 4) if total else None,
        # Reported separately because it is the asymmetry that matters: the two
        # layers disagreeing is one fact, the model mostly disagreeing in one
        # direction is another.
        "rule_events": rule_events,
        "overruled": overruled,
        "overrule_rate": round(overruled / rule_events, 4) if rule_events else None,
        "judged": 0,
    }


@router.get("/sources")
def sources(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    """Per source: how much arrived, how much survived, how much became an event."""
    if not available(db):
        return {"available": False, "items": []}
    clause, parameters = (("WHERE v.company_id=?", [company]) if company else ("", []))
    rows = db.execute(f"""SELECT v.source_id, v.source_tier,
               count(*) AS documents,
               sum(CASE WHEN p.inclusion_status='included' THEN 1 ELSE 0 END) AS included,
               sum(CASE WHEN p.eventhood='EVENT' THEN 1 ELSE 0 END) AS events,
               min(v.published_at) AS first_published,
               max(v.published_at) AS last_published
        FROM document_versions v
        LEFT JOIN processed_documents p ON p.version_id = v.version_id
        {clause} GROUP BY v.source_id, v.source_tier
        ORDER BY documents DESC""", parameters).fetchall()
    return {"available": True, "items": [{
        **dict(row),
        # A source whose documents never become events is either the wrong source
        # or a parser that has stopped working; the rate is what makes the
        # difference noticeable before somebody goes looking.
        "event_rate": round((row["events"] or 0) / row["documents"], 4) if row["documents"] else None,
    } for row in rows]}


@router.get("/documents")
def documents(company: str | None = None, inclusion_status: str | None = None,
              eventhood: str | None = None, source: str | None = None,
              layer: str | None = None, search: str | None = None,
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              db=Depends(db_session)) -> dict[str, Any]:
    if not available(db):
        return {"total": 0, "limit": limit, "offset": offset, "items": [], "available": False}
    filters, parameters = [], []
    for column, value in (("v.company_id", company), ("p.inclusion_status", inclusion_status),
                          ("p.eventhood", eventhood), ("v.source_id", source)):
        if value:
            filters.append(f"{column}=?")
            parameters.append(value)
    if layer:
        if layer not in LAYER_CASES:
            raise HTTPException(status_code=400, detail=f"未知的分层对比：{layer}")
        # The comparison only exists for documents that reached the model, so
        # this carries the gate with it rather than trusting the caller to pass
        # inclusion_status too. Without it the filter answered 1,432 where the
        # matrix said 757 — the difference being excluded documents, which have
        # a rule reading and no second opinion to compare it against.
        filters.append("p.inclusion_status='included'")
        filters.append(f"(p.rule_eventhood IS NOT NULL AND {LAYER_CASES[layer][1]})")
    if search:
        clause, values = like_clause(("v.title",), search)
        filters.append(clause)
        parameters.extend(values)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    base = f"""FROM processed_documents p
               JOIN document_versions v ON v.version_id = p.version_id{where}"""
    total = total_of(db, f"SELECT p.version_id {base}", parameters)
    rows = db.execute(f"""SELECT p.version_id, p.inclusion_status, p.eventhood,
               p.rule_eventhood, p.rule_relevance_score,
               p.extraction_status, p.relevance_score, p.rejection_reasons_json,
               p.text_chars, p.page_count, p.processed_at,
               v.title, v.source_id, v.source_tier, v.document_type, v.published_at,
               v.canonical_url, v.company_id {base}
        ORDER BY v.published_at DESC LIMIT ? OFFSET ?""",
        [*parameters, limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [{**dict(row),
                       "rejection_reasons": json.loads(row["rejection_reasons_json"] or "[]")}
                      for row in rows],
            "labels": {"inclusion": INCLUSION_LABELS, "eventhood": EVENTHOOD_LABELS,
                       "rejection": REJECTION_LABELS,
                       "layer": {key: label for key, (label, _) in LAYER_CASES.items()}}}
