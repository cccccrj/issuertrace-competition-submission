"""Event ledger endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db import connect, event_clock_sql, has_table, json_field, like_clause, paginate, total_of
from api.inquiry import chain as inquiry_chain
from api.labels import PUBLIC_BASES, public_causal_method, public_label_source, public_method
from api.sources import source_name

router = APIRouter(prefix="/api/events", tags=["events"])


def db_session():
    db = connect()
    try:
        yield db
    finally:
        db.close()


def event_row(row: Any) -> dict[str, Any]:
    # 计划/截止日期（I-04）：报道发布时尚在未来的事件时间不是发生时间。旧库没有这一列，按已发生读。
    role = row["event_time_role"] if "event_time_role" in row.keys() else None
    scheduled = role == "scheduled"
    return {
        "event_id": row["event_id"],
        "version_id": row["version_id"],
        "company_id": row["company_id"],
        "summary": row["summary"],
        "event_time": row["event_time"],
        "event_time_precision": row["event_time_precision"],
        "published_at": row["published_at"],
        # The timeline must be able to say which clock it used; an event time is
        # when something happened, a publish time is when someone wrote about it.
        "time_basis": "event_time" if row["event_time"] and not scheduled else "published_at",
        "event_time_role": role,
        "target_time": row["event_time"] if scheduled else None,
        "event_types": json_field(row["event_types_json"]),
        "issue_codes": json_field(row["issue_codes_json"]),
        "event_subjects": json_field(row["event_subjects_json"]),
        "affected_objects": json_field(row["affected_objects_json"]),
        "impact_direction": row["impact_direction"],
        "impact_magnitude": row["impact_magnitude"],
        "production_mode": row["production_mode"],
        "risk_score": row["risk_score"],
        "risk_level": row["risk_level"],
        "needs_review": bool(row["needs_review"]),
        "review_reasons": json_field(row["review_reasons_json"]),
        "title": row["title"],
        "source_id": row["source_id"],
        "source_name": source_name(row["source_id"]),
        "canonical_url": row["canonical_url"],
    }


BASE_QUERY = """SELECT e.*, v.title, v.source_id, v.canonical_url, v.published_at
                FROM event_records e JOIN document_versions v ON v.version_id=e.version_id"""


@router.get("")
def list_events(company: str | None = None, issue: str | None = None, risk_level: str | None = None,
                needs_review: bool | None = None, search: str | None = None,
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                db=Depends(db_session)) -> dict[str, Any]:
    filters, parameters = [], []
    if company:
        filters.append("e.company_id=?")
        parameters.append(company)
    if issue:
        # issue_codes is a JSON array; a LIKE on the quoted code avoids matching
        # a code that is merely a prefix of another.
        filters.append("e.issue_codes_json LIKE ?")
        parameters.append(f'%"{issue}"%')
    if risk_level:
        filters.append("e.risk_level=?")
        parameters.append(risk_level)
    if needs_review is not None:
        filters.append("e.needs_review=?")
        parameters.append(int(needs_review))
    if search:
        clause, values = like_clause(("e.summary", "v.title"), search)
        filters.append(clause)
        parameters.extend(values)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    # Events without a normalized time fall back to publication time so the
    # ledger stays in one chronological order instead of sinking them all.  A
    # deadline or target still in the future when reported sorts by when it was
    # reported: ordered by its date, a 2032 contract deadline topped the list (I-04).
    sort_time, _ = event_clock_sql(db)
    ordered = f"{BASE_QUERY}{where} ORDER BY {sort_time} DESC"
    query, bound = paginate(ordered, parameters, limit, offset)
    rows = db.execute(query, bound).fetchall()
    return {"total": total_of(db, f"{BASE_QUERY}{where}", parameters),
            "limit": limit, "offset": offset,
            "items": [event_row(row) for row in rows]}


# 每张窄口径 labels 表：表名 -> (值列, 该判据在界面上属于哪一块)
BASIS_TABLES = {
    "event_type_labels": "event_types_json",
    "event_impact_labels": "impact_direction",
    "production_mode_labels": "mode",
    "article_sentiment_labels": "sentiment",
}


def label_basis(db, event_id: str) -> dict[str, Any]:
    """Why each narrow classifier answered as it did, one block per axis.

    A block is ``None`` when that judgement has not been made for this event —
    ``production_mode_labels`` covers 1,033 of 1,907 events because announcements
    never enter that pass — and the page has to be able to say 「尚未覆盖」 rather
    than render a blank where a reason belongs.
    """
    basis: dict[str, Any] = {}
    for table, _ in BASIS_TABLES.items():
        key = table.removesuffix("_labels")
        if not has_table(db, table):
            basis[key] = None
            continue
        row = db.execute(f"SELECT * FROM {table} WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            basis[key] = None
            continue
        columns = row.keys()
        # 出自哪个模型、哪一版提示词留在库里，不出接口（I-06）。
        block = {"reason": row["reason"] if "reason" in columns else None,
                 "confidence": row["confidence"] if "confidence" in columns else None,
                 "basis": PUBLIC_BASES.get(row["basis"], row["basis"]) if "basis" in columns else None}
        if "magnitude_basis" in columns:
            block["magnitude_basis"] = row["magnitude_basis"]
        basis[key] = block
    return basis


@router.get("/{event_id}")
def get_event(event_id: str, db=Depends(db_session)) -> dict[str, Any]:
    row = db.execute(f"{BASE_QUERY} WHERE e.event_id=?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="event not found")
    detail = event_row(row)
    detail.update({
        "amounts": json_field(row["amounts_json"]),
        "ratios": json_field(row["ratios_json"]),
        "persons": json_field(row["persons_json"]),
        "organizations": json_field(row["organizations_json"]),
        "products": json_field(row["products_json"]),
        "article_sentiment": row["article_sentiment"],
        "recommended_action": row["recommended_action"],
        "extraction_confidence": row["extraction_confidence"],
        # Claims the model proposed and the span matcher refused. Until this was
        # recorded, 675 rejections across the corpus left no trace at all — the
        # count said a drop happened and nothing said whether the model was
        # paraphrasing (a correct rejection) or the matcher failed on text it
        # should have found. Absent on events labelled before it was kept.
        "dropped_claims": json_field(row["raw_label_json"], {}).get("dropped_claims") or [],
        # 规则判的还是模型判的。这一列在库里存着，此前连 API 都没发过。
        "label_source": public_label_source(row["label_source"]),
        # 风险分的七项加减。分数一直以一个数字示人，而同一页往下的可信度从第一天起就
        # 展示它的四项加权贡献——项目自己在 CausalEdgeRow 里判过「把计算过程当理由」的
        # 死刑，但风险分连计算过程都没给过。
        "risk_breakdown": json_field(row["risk_breakdown_json"], []),
        # 「这一类凭什么」。四张 labels 表每行都带 reason / confidence / basis /
        # model_name / prompt_version，profiling 页一直在用，而事件详情第一屏从不 JOIN
        # 它们——「疑似宣发」四个字贴在一家公司身上而不说为什么，是最容易被点名的一处。
        # 列表接口不加：一页 50 条，四个 JOIN 是白付的钱。
        "basis": label_basis(db, event_id),
    })
    # 证据文档的标题与地址此前一样都没取：⑤ 区只印得出「第 N 页 · 某章节」，
    # 连引的是哪份文件都没说，更点不开。而 canonical_url 在库里覆盖率是 100%。
    claims = db.execute("""SELECT c.*, v.verdict, v.partial_subtype, v.inquiry_question_id,
                                  v.evidence_page, v.evidence_section,
                                  v.evidence_text, v.evidence_version_id, v.reason, v.method, v.confidence
                                  AS verdict_confidence,
                                  d.title AS evidence_title, d.canonical_url AS evidence_url
        FROM event_claims c LEFT JOIN claim_verdicts v ON v.claim_id=c.claim_id
        LEFT JOIN document_versions d ON d.version_id=v.evidence_version_id
        WHERE c.event_id=? ORDER BY c.ordinal""", (event_id,)).fetchall()
    detail["claims"] = [{
        "claim_id": claim["claim_id"], "text": claim["text"], "issue_code": claim["issue_code"],
        "claim_type": claim["claim_type"], "confidence": claim["confidence"],
        "verdict": claim["verdict"], "partial_subtype": claim["partial_subtype"],
        "evidence_page": claim["evidence_page"], "evidence_section": claim["evidence_section"],
        "evidence_text": claim["evidence_text"], "evidence_version_id": claim["evidence_version_id"],
        "evidence_title": claim["evidence_title"], "evidence_url": claim["evidence_url"],
        "verdict_reason": claim["reason"], "verdict_method": public_method(claim["method"]),
        "verdict_confidence": claim["verdict_confidence"],
        # Only the claims that were settled by an inquiry carry the chain, so a
        # page of ordinary claims costs nothing extra.
        "inquiry": inquiry_chain(db, claim["inquiry_question_id"]),
    } for claim in claims]
    # The detail page has to answer the brief's seven outputs in one place, and
    # three of them — credibility, the timeline entry and the alert rationale —
    # live in the analysis tables rather than on the event row.  Joining them
    # here keeps the page from re-deriving anything.
    # The standardisation half of task two, on the page that has to answer
    # the brief's seven outputs. The flattened JSON arrays above keep the
    # values and drop whether each one resolved, in what unit and on what
    # basis — the part that makes an extracted figure checkable.
    has_elements = has_table(db, "event_elements")
    detail["elements"] = [dict(element) for element in db.execute(
        """SELECT element_type, ordinal, raw_text, normalized_value, unit, basis, status
           FROM event_elements WHERE event_id=? ORDER BY element_type, ordinal""",
        (event_id,))] if has_elements else []
    timeline = db.execute("SELECT * FROM event_timeline WHERE event_id=?", (event_id,)).fetchone()
    # 构建轮次的内部编号不出接口。
    detail["timeline"] = ({key: value for key, value in dict(timeline).items()
                           if key not in ("first_seen_round", "is_new_this_round", "updated_at")}
                          if timeline else None)
    # The only external check on 影响方向 anywhere in this system. It is a
    # consistency comparison and not a cause, and the page has to say so — a
    # price move is evidence about the tape, not about the document.
    # Absent for every event outside the price series, which for a 2026 listing
    # is most of its corpus; that gap is shown rather than filled.
    detail["market_reaction"] = None
    if has_table(db, "event_market_reaction"):
        reaction = db.execute("""SELECT symbol, benchmark_symbol, anchor_date, anchor_basis,
                                        baseline_date, car_1, car_3, car_5, sigma,
                                        observed_direction, significant, agrees
                                 FROM event_market_reaction WHERE event_id=?""",
                              (event_id,)).fetchone()
        detail["market_reaction"] = dict(reaction) if reaction else None
    alerts = db.execute("""SELECT alert_id, headline, risk_level, adjusted_risk_score, credibility,
                                  credibility_label, credibility_basis_json, reasons_json,
                                  window_start, window_end, event_count, issue_code
        FROM alerts WHERE event_ids_json LIKE ? ORDER BY adjusted_risk_score DESC""",
        (f'%"{event_id}"%',)).fetchall()
    detail["alerts"] = [{**{key: alert[key] for key in alert.keys()
                           if not key.endswith("_json")},
                         "credibility_basis": json_field(alert["credibility_basis_json"], {}),
                         "reasons": json_field(alert["reasons_json"])} for alert in alerts]
    components = db.execute("""SELECT c.component_id, c.member_count, c.source_count,
                                      c.first_published_at, c.last_published_at
        FROM propagation_component_members m
        JOIN propagation_components c ON c.component_id=m.component_id
        WHERE m.version_id=?""", (row["version_id"],)).fetchall()
    detail["propagation"] = [dict(component) for component in components]
    # 这一页此前对同源簇只字不提——`duplicate` 在本文件里出现 0 次，于是从一条事件
    # 出发根本走不到「这篇稿子还有几个近重复版本、分别是哪些」。同样的子查询在
    # api/routes/meta.py 的指南接口里已经写过一遍（cluster_expr），落地页也早为
    # `?cluster=` 绕开了 member_count 水位，缺的只是把它接到这里。
    if has_table(db, "duplicate_cluster_members"):
        cluster = db.execute("""SELECT c.cluster_id, c.member_count,
                   (SELECT count(DISTINCT v.source_id)
                      FROM duplicate_cluster_members m2
                      JOIN document_versions v ON v.version_id=m2.version_id
                     WHERE m2.cluster_id=c.cluster_id) AS distinct_sources
            FROM duplicate_cluster_members m
            JOIN duplicate_clusters c ON c.cluster_id=m.cluster_id
            WHERE m.version_id=? ORDER BY c.cluster_id LIMIT 1""",
            (row["version_id"],)).fetchone()
        detail["duplicate_cluster"] = dict(cluster) if cluster else None
    else:
        detail["duplicate_cluster"] = None
    # What led to this event and what followed from it.  The component above
    # answers a different question — who copied whom — and until this block
    # existed the page could not answer the first one at all: a reader wanting
    # the story around an event had to go back to the whole timeline and find
    # its neighbours by eye.  CONTEXT_ONLY edges are the layer's refusals, and a
    # refusal is not a link, so they stay in the table and off the page.
    detail["causal_edges"] = (causal_edges(db, event_id)
                              if has_table(db, "event_causal_edges") else [])
    return detail


def causal_edges(db: Any, event_id: str) -> list[dict[str, Any]]:
    rows = db.execute("""SELECT c.edge_id, c.relation, c.confidence, c.reason, c.quote_from,
                                c.quote_to, c.method, c.gap_hours, c.time_precision_low,
                                CASE WHEN c.from_event_id=:id THEN 'effect' ELSE 'cause' END side,
                                CASE WHEN c.from_event_id=:id THEN c.to_event_id
                                     ELSE c.from_event_id END other_event_id,
                                e.summary, e.event_time, e.risk_level, v.title, v.published_at
        FROM event_causal_edges c
        JOIN event_records e ON e.event_id = CASE WHEN c.from_event_id=:id
                                                  THEN c.to_event_id ELSE c.from_event_id END
        JOIN document_versions v ON v.version_id=e.version_id
        WHERE (c.from_event_id=:id OR c.to_event_id=:id) AND c.relation<>'CONTEXT_ONLY'
        ORDER BY ABS(c.gap_hours)""", {"id": event_id}).fetchall()
    return [{**dict(row), "method": public_causal_method(row["method"])} for row in rows]
