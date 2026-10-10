#!/usr/bin/env python3
"""Cross-company migration evidence (E7).

The acceptance standard is "a second company runs with zero code changes, and
there are numbers for it".  This assembles those numbers from the database, for
a reference company and a target, so the comparison is produced the same way
every time instead of being re-derived by hand for a slide.

Two rules shape what it reports.  Anything that needs a human label is *not*
reported as accuracy — subject disambiguation and AH document pairing both need
ground truth, and inventing a number for them is exactly the failure the review
queue exists to prevent; they are listed under ``gaps`` with what is missing.
Everything else is a distribution the two companies can be compared on, and a
gap in a distribution is stated as a gap, not as a degradation: a corpus that is
59% traditional Chinese explains a lower citation rate without the pipeline
being any worse.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any

from collector.collect import connect
from processor.pipeline import configured_companies, utc_now

# Measurements that would need a labelled sample before they mean anything.
# Naming them here keeps the report honest about its own coverage.
UNMEASURABLE = [
    {"metric": "主体消歧准确率", "needs": "人工标注的主体—提及对照集",
     "available": "解析覆盖率（已归一化提及 / 全部提及），不是准确率"},
    {"metric": "中英文/AH 文档配对准确率", "needs": "人工确认的 A 股—H 股文档对",
     "available": "各语言语料规模，以及配对引擎当前产出的版本对数量"},
    {"metric": "E1/E4 相对参照公司的下降幅度", "needs": "两家公司各自的人工评测集",
     "available": "四值裁决分布与引用率的差异，属分布差异而非准确率差异"},
    {"metric": "问询三元组在目标公司的表现", "needs": "目标公司存在问询函回复类披露文件",
     "available": "各公司的问询语料规模与已对齐问题数；港交所披露制度中没有对应文件，"
                  "这是制度差异而非实现退化"},
]


def counts(db: sqlite3.Connection, query: str, company: str) -> dict[str, int]:
    return {str(row[0]): row[1] for row in db.execute(query, (company,))}


def corpus(db: sqlite3.Connection, company: str) -> dict[str, Any]:
    versions = db.execute("""SELECT count(*) FROM document_versions v
        JOIN discovered_metadata m ON m.canonical_url=v.canonical_url
        WHERE m.company_id=?""", (company,)).fetchone()[0]
    paragraphs = db.execute("SELECT count(*) FROM evidence_paragraphs WHERE company_id=?",
                            (company,)).fetchone()[0]
    languages = counts(db, """SELECT COALESCE(p.language,'unknown'), count(*)
        FROM evidence_paragraphs e JOIN document_versions p ON p.version_id=e.version_id
        WHERE e.company_id=? GROUP BY 1""", company)
    return {"discovered_versions": versions, "evidence_paragraphs": paragraphs,
            "evidence_documents": db.execute(
                "SELECT count(DISTINCT version_id) FROM evidence_paragraphs WHERE company_id=?",
                (company,)).fetchone()[0],
            "evidence_paragraphs_by_language": languages,
            "document_types": counts(db, """SELECT COALESCE(document_type,'unknown'), count(*)
                FROM evidence_paragraphs WHERE company_id=? GROUP BY 1""", company)}


def events(db: sqlite3.Connection, company: str) -> dict[str, Any]:
    total, flagged = db.execute("""SELECT count(*), COALESCE(sum(needs_review),0)
        FROM event_records WHERE company_id=?""", (company,)).fetchone()
    claims = db.execute("""SELECT count(*) FROM event_claims c
        JOIN event_records e ON e.event_id=c.event_id WHERE e.company_id=?""",
        (company,)).fetchone()[0]
    without = db.execute("""SELECT count(*) FROM event_records e WHERE e.company_id=?
        AND NOT EXISTS (SELECT 1 FROM event_claims c WHERE c.event_id=e.event_id)""",
        (company,)).fetchone()[0]
    return {"events": total, "needs_review": flagged, "claims": claims,
            "events_without_claims": without,
            "issue_codes": sorted(counts(db, """SELECT c.issue_code, count(*) FROM event_claims c
                JOIN event_records e ON e.event_id=c.event_id
                WHERE e.company_id=? AND c.issue_code IS NOT NULL GROUP BY 1""", company))}


def subjects(db: sqlite3.Connection, company: str) -> dict[str, Any]:
    """Coverage of subject normalisation — how many mentions resolved at all.

    Not accuracy: whether each resolution is *right* is what the evaluation set
    is for.  Reporting coverage as accuracy is the mistake this project keeps
    refusing to make, so the field is named for what it measures.
    """
    resolved = counts(db, """SELECT el.status, count(*) FROM event_elements el
        JOIN event_records e ON e.event_id=el.event_id
        WHERE e.company_id=? AND el.element_type='subject' GROUP BY 1""", company)
    total = sum(resolved.values())
    return {**resolved, "mentions": total,
            "resolution_coverage": round(resolved.get("normalized", 0) / total, 4) if total else None}


def verdicts(db: sqlite3.Connection, company: str) -> dict[str, Any]:
    mix = counts(db, "SELECT verdict, count(*) FROM claim_verdicts WHERE company_id=? GROUP BY 1",
                 company)
    methods = counts(db, "SELECT method, count(*) FROM claim_verdicts WHERE company_id=? GROUP BY 1",
                     company)
    attempted = sum(mix.values()) - methods.get("no_corpus", 0)
    cited = sum(mix.get(key, 0) for key in ("SUPPORTED", "PARTIAL", "CONFLICT"))
    return {"verdicts": mix, "methods": methods, "attempted": attempted,
            "cited_rate": round(cited / attempted, 4) if attempted else None,
            "unverifiable_share": round(mix.get("UNVERIFIABLE", 0) / attempted, 4) if attempted else None,
            "settled_by_inquiry": db.execute("""SELECT count(*) FROM claim_verdicts
                WHERE company_id=? AND inquiry_question_id IS NOT NULL""", (company,)).fetchone()[0]}


def inquiry(db: sqlite3.Connection, company: str) -> dict[str, Any]:
    """What the inquiry route found for this company, which may be nothing.

    Mainland review letters have no counterpart in a Hong Kong filing, so a
    company listed there yields no questions at all.  That is a difference
    between disclosure regimes, not a pipeline that got worse on the second
    company, and the report has to be able to say which of the two it is.
    """
    if not db.execute("""SELECT name FROM sqlite_master WHERE type='table'
                         AND name='inquiry_questions'""").fetchone():
        return {"questions": 0, "triplets": 0, "verdicts_settled": 0, "available": False}
    questions = db.execute("SELECT count(*) FROM inquiry_questions WHERE company_id=?",
                           (company,)).fetchone()[0]
    return {"questions": questions,
            "triplets": db.execute("SELECT count(*) FROM inquiry_triplets WHERE company_id=?",
                                   (company,)).fetchone()[0],
            "verdicts_settled": db.execute("""SELECT count(*) FROM claim_verdicts
                WHERE company_id=? AND inquiry_question_id IS NOT NULL""", (company,)).fetchone()[0],
            "available": bool(questions),
            "corpus_documents": db.execute("""SELECT count(DISTINCT version_id)
                FROM evidence_paragraphs WHERE company_id=? AND document_type='review_responses'""",
                (company,)).fetchone()[0]}


def aggregation(db: sqlite3.Connection, company: str) -> dict[str, Any]:
    return {"duplicate_clusters": db.execute(
                "SELECT count(*) FROM duplicate_clusters WHERE company_id=?", (company,)).fetchone()[0],
            "propagation_edges": counts(db, """SELECT edge_kind, count(*) FROM propagation_edges
                WHERE company_id=? GROUP BY 1""", company),
            "issue_ledger": db.execute("SELECT count(*) FROM issue_ledger WHERE company_id=?",
                                       (company,)).fetchone()[0],
            "disclosure_diffs": db.execute("SELECT count(*) FROM disclosure_diffs WHERE company_id=?",
                                           (company,)).fetchone()[0],
            "alerts": db.execute("SELECT count(*) FROM alerts WHERE company_id=?",
                                 (company,)).fetchone()[0]}


def pipeline(db: sqlite3.Connection, company: str) -> dict[str, Any]:
    """Which code produced this company's results.

    The migration claim is that the same processor ran on both companies.  The
    version stamped on every event is what makes that checkable rather than
    asserted.
    """
    return {"processor_versions": counts(db, """SELECT p.processor_version, count(*)
                FROM event_records e JOIN processed_documents p ON p.version_id=e.version_id
                WHERE e.company_id=? GROUP BY 1""", company),
            "models": counts(db, "SELECT COALESCE(model_name,'-'), count(*) FROM event_records "
                                 "WHERE company_id=? GROUP BY 1", company),
            "verdict_prompts": counts(db, """SELECT COALESCE(prompt_version,'-'), count(*)
                FROM claim_verdicts WHERE company_id=? GROUP BY 1""", company)}


def profile(db: sqlite3.Connection, company: str) -> dict[str, Any]:
    return {"corpus": corpus(db, company), "events": events(db, company),
            "subjects": subjects(db, company), "verdicts": verdicts(db, company),
            "inquiry": inquiry(db, company), "aggregation": aggregation(db, company),
            "pipeline": pipeline(db, company)}


def comparison(reference: dict[str, Any], target: dict[str, Any],
               reference_name: str = "reference", target_name: str = "target") -> dict[str, Any]:
    """What the second company reused, and where it stands relative to the first."""
    reference_codes = set(reference["events"]["issue_codes"])
    target_codes = set(target["events"]["issue_codes"])
    shared = reference_codes & target_codes
    return {
        "issue_labels_reused": sorted(shared),
        "issue_labels_new_for_target": sorted(target_codes - reference_codes),
        "issue_reuse_rate": round(len(shared) / len(target_codes), 4) if target_codes else None,
        "cited_rate_gap": round(target["verdicts"]["cited_rate"] - reference["verdicts"]["cited_rate"], 4)
        if target["verdicts"]["cited_rate"] is not None
        and reference["verdicts"]["cited_rate"] is not None else None,
        "subject_coverage_gap": round(target["subjects"]["resolution_coverage"]
                                      - reference["subjects"]["resolution_coverage"], 4)
        if target["subjects"]["resolution_coverage"] is not None
        and reference["subjects"]["resolution_coverage"] is not None else None,
        "same_processor_version": sorted(reference["pipeline"]["processor_versions"])
        == sorted(target["pipeline"]["processor_versions"]),
        "same_verdict_prompt": sorted(reference["pipeline"]["verdict_prompts"])
        == sorted(target["pipeline"]["verdict_prompts"]),
        # A capability with no material on the second company is a fact about
        # its disclosure regime; reported as such rather than as a shortfall.
        "inquiry_alignment_available": {
            reference_name: reference["inquiry"]["available"],
            target_name: target["inquiry"]["available"]},
    }


def run(reference: str | None = None, target: str | None = None) -> dict[str, Any]:
    companies = configured_companies()
    reference, target = reference or companies[0], target or companies[-1]
    db = connect()
    db.row_factory = sqlite3.Row
    profiles = {reference: profile(db, reference), target: profile(db, target)}
    db.close()
    return {"generated_at": utc_now(), "reference": reference, "target": target,
            "profiles": profiles,
            "comparison": comparison(profiles[reference], profiles[target], reference, target),
            "gaps": UNMEASURABLE}


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-company migration evidence (E7)")
    parser.add_argument("--reference", choices=configured_companies())
    parser.add_argument("--target", choices=configured_companies())
    args = parser.parse_args()
    print(json.dumps(run(args.reference, args.target), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
