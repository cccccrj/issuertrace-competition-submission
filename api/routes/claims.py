"""Claim verification endpoints — the four-valued verdict and its citation."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db import has_column, json_field, like_clause, paginate, total_of
from api.labels import (METHOD_LABELS, UNVERIFIABLE_GROUPS, UNVERIFIABLE_SUBTYPE_GROUPS,
                        UNVERIFIABLE_SUBTYPE_LABELS, internal_method, public_method,
                        public_numeric_check)
from api.inquiry import chain as inquiry_chain
from api.routes.events import db_session

router = APIRouter(prefix="/api/claims", tags=["claims"])

BASE_QUERY = """SELECT c.claim_id, c.text, c.issue_code, c.claim_type, c.confidence AS claim_confidence,
                       e.event_id, e.company_id, e.summary AS event_summary, e.event_time,
                       v.verdict, v.partial_subtype, {subtype_columns}, {numeric_column},
                       v.evidence_version_id, v.evidence_page,
                       v.evidence_section, v.evidence_text, v.retrieval_score, v.method,
                       v.confidence AS verdict_confidence, v.reason, v.adjudicated_at,
                       v.inquiry_question_id,
                       d.title AS evidence_title, d.document_type AS evidence_document_type,
                       d.canonical_url AS evidence_url
                FROM event_claims c
                JOIN event_records e ON e.event_id=c.event_id
                LEFT JOIN claim_verdicts v ON v.claim_id=c.claim_id
                LEFT JOIN document_versions d ON d.version_id=v.evidence_version_id"""


def base_query(db) -> str:
    """无法核验子类两列由 ``processor.unverifiable`` 补上；只读连接补不了列，旧库上按空值读。"""
    columns = ("v.unverifiable_subtype, v.unverifiable_basis"
               if has_column(db, "claim_verdicts", "unverifiable_subtype")
               else "NULL AS unverifiable_subtype, NULL AS unverifiable_basis")
    # 数值核对由 ``processor.claim_verdict`` 补列；旧库上同样按空值读。
    numeric = ("v.numeric_check" if has_column(db, "claim_verdicts", "numeric_check")
               else "NULL AS numeric_check")
    return BASE_QUERY.format(subtype_columns=columns, numeric_column=numeric)


def claim_row(row: Any) -> dict[str, Any]:
    return {
        "claim_id": row["claim_id"], "text": row["text"], "issue_code": row["issue_code"],
        "claim_type": row["claim_type"], "claim_confidence": row["claim_confidence"],
        "event_id": row["event_id"], "company_id": row["company_id"],
        "event_summary": row["event_summary"], "event_time": row["event_time"],
        "verdict": row["verdict"], "partial_subtype": row["partial_subtype"],
        # 无法核验的是哪一种：设计边界，还是可以改进。依据随子类一起给，读者可以不同意某一条规则。
        "unverifiable_subtype": row["unverifiable_subtype"],
        "unverifiable_label": UNVERIFIABLE_SUBTYPE_LABELS.get(row["unverifiable_subtype"]),
        "unverifiable_group": UNVERIFIABLE_SUBTYPE_GROUPS.get(row["unverifiable_subtype"]),
        "unverifiable_basis": row["unverifiable_basis"],
        "inquiry_question_id": row["inquiry_question_id"],
        "method": public_method(row["method"]), "method_label": METHOD_LABELS.get(public_method(row["method"])),
        "verdict_confidence": row["verdict_confidence"],
        "reason": row["reason"], "adjudicated_at": row["adjudicated_at"],
        # 换算与差值由程序算，不由模型算（I-02）；与模型结论相左时 model_disputed 为真。
        "numeric_check": public_numeric_check(json_field(row["numeric_check"])),
        "evidence": {
            "version_id": row["evidence_version_id"], "title": row["evidence_title"],
            "document_type": row["evidence_document_type"], "page": row["evidence_page"],
            "section": row["evidence_section"], "text": row["evidence_text"],
            # 这一列此前没取。整条证据路径都没取——四个接口都 JOIN 了
            # document_versions，却只要 title 和 document_type，于是页面上《文件名》
            # 第 N 页永远是一段纯文本，而「可核验」正是这一步该兑现的地方。
            "url": row["evidence_url"],
        } if row["evidence_version_id"] else None,
    }


@router.get("")
def list_claims(company: str | None = None, verdict: str | None = None, issue: str | None = None,
                method: str | None = None, search: str | None = None,
                partial_subtype: str | None = None, event: str | None = None,
                unverifiable_subtype: str | None = None, unverifiable_group: str | None = None,
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                db=Depends(db_session)) -> dict[str, Any]:
    filters, parameters = [], []
    if company:
        filters.append("e.company_id=?")
        parameters.append(company)
    # 「这一条事件的主张是怎么核验的」此前在本接口上问不出来：事件详情页把它们
    # 摆在自己那一页，但没有一条 URL 能把主张核验页收窄到一个事件。指南的第三步
    # 要的正是这个，而 event_id 早就在 BASE_QUERY 的连接里。
    if event:
        filters.append("c.event_id=?")
        parameters.append(event)
    if verdict:
        filters.append("v.verdict=?" if verdict != "PENDING" else "v.verdict IS NULL")
        if verdict != "PENDING":
            parameters.append(verdict)
    if issue:
        filters.append("c.issue_code=?")
        parameters.append(issue)
    if method:
        filters.append("v.method=?")
        parameters.append(internal_method(method))
    # The subtype is what separates "the exchange already asked and the company
    # already answered" from an unexplained partial, and the demonstration
    # script asks for exactly that slice; it was returned by this endpoint but
    # never filterable, which made the beat unperformable in the UI.
    if partial_subtype:
        filters.append("v.partial_subtype=?")
        parameters.append(partial_subtype)
    if unverifiable_subtype and unverifiable_subtype not in UNVERIFIABLE_SUBTYPE_LABELS:
        raise HTTPException(status_code=400, detail=f"未知的无法核验子类：{unverifiable_subtype}")
    codes = [code for code, group in UNVERIFIABLE_SUBTYPE_GROUPS.items() if group == unverifiable_group]
    if unverifiable_group and not codes:
        raise HTTPException(status_code=400, detail=f"未知的无法核验分组：{unverifiable_group}")
    split = has_column(db, "claim_verdicts", "unverifiable_subtype")
    if (unverifiable_subtype or unverifiable_group) and not split:
        # 列还没补上：一条都没有，而不是「筛不了，于是全给你」。
        filters.append("1=0")
    else:
        if unverifiable_subtype:
            filters.append("v.unverifiable_subtype=?")
            parameters.append(unverifiable_subtype)
        if unverifiable_group:
            filters.append(f"v.unverifiable_subtype IN ({','.join('?' * len(codes))})")
            parameters.extend(codes)
    base = base_query(db)
    if search:
        clause, values = like_clause(("c.text",), search)
        filters.append(clause)
        parameters.extend(values)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    # Cited verdicts first: a claim the platform can prove is worth more screen
    # space than one it merely could not check.
    ordered = (f"{base}{where} ORDER BY CASE v.verdict WHEN 'CONFLICT' THEN 0 WHEN 'PARTIAL' THEN 1"
               " WHEN 'SUPPORTED' THEN 2 WHEN 'UNVERIFIABLE' THEN 3 ELSE 4 END, v.confidence DESC")
    query, bound = paginate(ordered, parameters, limit, offset)
    return {"total": total_of(db, f"{base}{where}", parameters),
            "limit": limit, "offset": offset,
            "items": [claim_row(row) for row in db.execute(query, bound).fetchall()]}


@router.get("/stats")
def claim_stats(company: str | None = None, db=Depends(db_session)) -> dict[str, Any]:
    """Verdict mix, reported so that unverifiable-for-lack-of-corpus stays visible.

    Folding ``no_corpus`` into a plain "unverifiable" count would let a company
    whose filings were never collected look like one the system checked and
    could not confirm.
    """
    clause, parameters = ("WHERE e.company_id=?", [company]) if company else ("", [])
    rows = db.execute(f"""SELECT v.verdict, v.method, count(*) AS total FROM event_claims c
        JOIN event_records e ON e.event_id=c.event_id
        LEFT JOIN claim_verdicts v ON v.claim_id=c.claim_id {clause}
        GROUP BY v.verdict, v.method""", parameters).fetchall()
    verdicts: dict[str, int] = {}
    methods: dict[str, int] = {}
    pending = no_corpus = 0
    for row in rows:
        if row["verdict"] is None:
            pending += row["total"]
            continue
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + row["total"]
        methods[public_method(row["method"])] = methods.get(public_method(row["method"]), 0) + row["total"]
        if row["method"] == "no_corpus":
            no_corpus += row["total"]
    adjudicated = sum(verdicts.values())
    # Accuracy-style rates exclude claims with no corpus to check against, so the
    # denominator is claims the system actually attempted.
    attempted = adjudicated - no_corpus
    return {"company": company, "total_claims": adjudicated + pending, "adjudicated": adjudicated,
            "unverifiable": unverifiable_breakdown(db, clause, parameters),
            "pending": pending, "no_corpus": no_corpus, "attempted": attempted,
            "verdicts": verdicts, "methods": methods,
            "cited_rate": round(sum(verdicts.get(key, 0) for key in ("SUPPORTED", "PARTIAL", "CONFLICT"))
                                / attempted, 4) if attempted else None}


def unverifiable_breakdown(db, clause: str, parameters: list[Any]) -> dict[str, Any]:
    """无法核验按子类与分组的构成。列还没补上时照实说「未拆分」，不拿 0 冒充。"""
    if not has_column(db, "claim_verdicts", "unverifiable_subtype"):
        return {"available": False, "subtypes": [], "groups": []}
    joiner = "AND" if clause else "WHERE"
    counts = dict(db.execute(f"""SELECT v.unverifiable_subtype, count(*) FROM event_claims c
        JOIN event_records e ON e.event_id=c.event_id
        JOIN claim_verdicts v ON v.claim_id=c.claim_id {clause}
        {joiner} v.verdict='UNVERIFIABLE' GROUP BY 1""", parameters).fetchall())
    subtypes = [{"code": code, "label": UNVERIFIABLE_SUBTYPE_LABELS[code],
                 "group": UNVERIFIABLE_SUBTYPE_GROUPS[code], "count": counts.get(code, 0)}
                for code in UNVERIFIABLE_SUBTYPE_LABELS]
    groups = [{"code": group, "label": label,
               "count": sum(row["count"] for row in subtypes if row["group"] == group)}
              for group, label in UNVERIFIABLE_GROUPS.items()]
    return {"available": True, "subtypes": subtypes, "groups": groups,
            # 还没拆的（新裁决之后没跑 finalize）单独数出来，不混进任何一类。
            "unclassified": counts.get(None, 0)}


@router.get("/{claim_id}")
def get_claim(claim_id: str, db=Depends(db_session)) -> dict[str, Any]:
    row = db.execute(f"{base_query(db)} WHERE c.claim_id=?", (claim_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="claim not found")
    detail = claim_row(row)
    if detail["evidence"]:
        # If the cited passage also changed between filings, the diff engine
        # already knows where; surfacing it turns a verdict into a timeline.
        changes = db.execute("""SELECT pair_key,section,left_page,right_page,diff_kind,metric,
                                       old_value,new_value FROM disclosure_diffs
            WHERE company_id=? AND section=? ORDER BY pair_key LIMIT 20""",
            (row["company_id"], row["evidence_section"])).fetchall()
        detail["evidence_section_changes"] = [dict(change) for change in changes]
    detail["inquiry"] = inquiry_chain(db, row["inquiry_question_id"])
    return detail


