#!/usr/bin/env python3
"""Align exchange inquiries to the answers that settle them and the disclosure
changes they produced.

A media claim that repeats something the exchange already asked about, the
company already answered and the filing already changed is not an open
question — but the verdict layer had no way to say so.  Retrieval could find
the answering paragraph, yet nothing recorded that the paragraph *was* an
answer, to which question, or what it changed in the prospectus.  The verdict
came out PARTIAL or UNVERIFIABLE and the platform could not tell the reader
"this was explained, in the reply to question 13, on page 167".

The alignment is deterministic on purpose.  Which question a paragraph belongs
to is a fact about the document's structure, not a judgement, and the link to a
disclosure change is scored by shared rare terms so the reason for every pair
can be printed and argued with.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from typing import Any

from collector.collect import audit, connect
from processor.claim_index import COMMON_TERM_SHARE, bigrams
from processor.pipeline import configured_companies, utc_now

# ``13．关于募投项目根据申请材料：（1）……`` — the number, then the topic, which runs
# until the question body starts.  Both the full-width and ASCII stops appear in
# the same corpus, and the second round writes ``1. 关于`` with a space.
QUESTION_HEADING = re.compile(r"^\s*(\d{1,2})\s*[．.、]\s*关于(.+?)(?=根据|请发行人|请保荐|$)")
# Replies are laid out as ``（一）``/``（二）`` mirroring the sub-questions asked.
ANSWER_HEADING = re.compile(r"^\s*[（(][一二三四五六七八九十]+[)）]")
ROUNDS = (("落实函", "implementation"), ("第二轮", "second"), ("第三轮", "third"),
          ("首轮", "first"), ("第一轮", "first"))
ROUND_LABELS = {"first": "首轮问询", "second": "第二轮问询", "third": "第三轮问询",
                "implementation": "落实函", "other": "问询"}
# A long question shares a few characters with almost any paragraph, so a pair
# has to clear both bars: enough shared rare terms to be more than coincidence,
# and enough of the question's own vocabulary to be about the same subject.
# Measured on the first round: relevant changes land at 0.06–0.09, unrelated
# ones below 0.04.  The count is capped against the question's own size — a
# dozen terms is a low bar for a question spanning thirty pages and an
# impossible one for a two-sentence question, and both are real.
MIN_SHARED_TERMS = 12
SHORT_QUESTION_SHARE = 3
MIN_MATCH_SCORE = 0.05
DIFFS_PER_QUESTION = 5
# The glossary, the table of contents and the signature pages restate every
# defined term in the document, so they match every question and answer none.
EXCLUDED_DIFF_SECTIONS = ("释义", "目录", "声明", "封面", "扉页")
# A one- or two-digit token is a row number, a list marker or a year fragment
# lifted out of a table.  Two passages sharing "4" have nothing in common, and
# printing it as the reason they were paired makes the alignment look arbitrary.
# Three digits and up — 295, 579 — are the figures the filings argue about.
SHORT_NUMBER = re.compile(r"^\d{1,2}$")
QUESTION_TEXT_CHARS = 600


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS inquiry_questions(
        question_id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL,
        version_id TEXT NOT NULL,
        round TEXT NOT NULL,
        document_title TEXT NOT NULL,
        number INTEGER NOT NULL,
        topic TEXT NOT NULL,
        question_text TEXT NOT NULL,
        question_page INTEGER,
        first_paragraph_id INTEGER NOT NULL,
        last_paragraph_id INTEGER NOT NULL,
        answer_first_paragraph_id INTEGER,
        answer_page INTEGER,
        aligned_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS inquiry_questions_span
        ON inquiry_questions(version_id, first_paragraph_id, last_paragraph_id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS inquiry_triplets(
        triplet_id TEXT PRIMARY KEY,
        question_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        response_paragraph_id INTEGER NOT NULL,
        response_page INTEGER,
        response_excerpt TEXT NOT NULL,
        diff_id TEXT NOT NULL,
        diff_section TEXT,
        diff_kind TEXT,
        shared_terms TEXT NOT NULL,
        match_score REAL NOT NULL,
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS inquiry_triplets_question
        ON inquiry_triplets(question_id)""")
    db.commit()


def identifier(*parts: Any) -> str:
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()[:20]


def round_of(title: str) -> str:
    for marker, name in ROUNDS:
        if marker in title:
            return name
    return "other"


def response_documents(db: sqlite3.Connection, companies: set[str] | None) -> list[sqlite3.Row]:
    clause, parameters = "", []
    if companies:
        clause = f" AND company_id IN ({','.join('?' for _ in companies)})"
        parameters = sorted(companies)
    return db.execute(f"""SELECT DISTINCT p.version_id, p.company_id, p.title
        FROM evidence_paragraphs p JOIN document_versions v ON v.version_id=p.version_id
        WHERE v.document_type='review_responses'{clause}
        ORDER BY p.version_id""", parameters).fetchall()


def parse_questions(rows: list[sqlite3.Row], document: sqlite3.Row) -> list[dict[str, Any]]:
    """Split one reply document into the questions it answers.

    A question owns everything from its own heading to the next one; that is how
    the document is written, and it means a paragraph can be attributed to a
    question without guessing.  Headings the extractor lost — question 4 and 6
    of the first round are absent from the text layer — simply do not appear,
    and the questions around them keep their own spans instead of swallowing the
    gap under a wrong number.
    """
    heads: list[tuple[int, int, str]] = []
    for index, row in enumerate(rows):
        match = QUESTION_HEADING.match(row["text"])
        if match:
            topic = match.group(2).strip("：: 　")
            heads.append((index, int(match.group(1)), topic[:40]))
    questions = []
    for position, (index, number, topic) in enumerate(heads):
        end = heads[position + 1][0] - 1 if position + 1 < len(heads) else len(rows) - 1
        block = rows[index:end + 1]
        answer = next((row for row in block[1:] if ANSWER_HEADING.match(row["text"])), None)
        # Everything before the reply is the question as the exchange put it.
        asked = "".join(row["text"] for row in block[:block.index(answer)] if row) if answer else block[0]["text"]
        questions.append({
            "question_id": identifier("inquiry", document["version_id"], number),
            "company_id": document["company_id"], "version_id": document["version_id"],
            "round": round_of(document["title"]), "document_title": document["title"],
            "number": number, "topic": topic, "question_text": asked[:QUESTION_TEXT_CHARS],
            "question_page": block[0]["page"],
            "first_paragraph_id": block[0]["paragraph_id"],
            "last_paragraph_id": block[-1]["paragraph_id"],
            "answer_first_paragraph_id": answer["paragraph_id"] if answer else None,
            "answer_page": answer["page"] if answer else None,
            "aligned_at": utc_now(),
            "paragraphs": block,
        })
    return questions


def vocabulary(db: sqlite3.Connection, company_id: str) -> dict[str, Any]:
    """Document frequency for one company's evidence, read once per run.

    Retrieval prunes a query to its twenty rarest terms because FTS has to merge
    a posting list per term.  Matching here is a set comparison in memory, where
    that cap would throw away most of the question: what is wanted is every term
    that is not boilerplate, not the twenty strangest.
    """
    total = db.execute("SELECT count(*) FROM evidence_paragraphs WHERE company_id=?",
                       (company_id,)).fetchone()[0] or 1
    frequency = dict(db.execute("SELECT token, documents FROM evidence_terms WHERE company_id=?",
                                (company_id,)).fetchall())
    return {"frequency": frequency, "ceiling": total * COMMON_TERM_SHARE}


def discriminating(text: str, vocab: dict[str, Any]) -> set[str]:
    """The terms of ``text`` that are not in most paragraphs already.

    Common bigrams — 公司, 发行, 报告 — appear in nearly every page of a
    prospectus and would pair every question with every change.
    """
    return {token for token in bigrams(text)
            if vocab["frequency"].get(token, 0) <= vocab["ceiling"]
            and not SHORT_NUMBER.match(token)}


def overlap(terms: set[str], text: str, vocab: dict[str, Any]) -> tuple[float, list[str]]:
    """How much of a question's own rare vocabulary a passage repeats.

    Stated as a share of the question rather than a symmetric similarity, so the
    number reads as "this much of what the question is about turns up in this
    change" — which is the claim the alignment actually makes.
    """
    if not terms:
        return 0.0, []
    shared = sorted(terms & discriminating(text, vocab))
    return len(shared) / len(terms), shared


def alignable(diff: sqlite3.Row) -> bool:
    section = diff["section"] or ""
    return not any(marker in section for marker in EXCLUDED_DIFF_SECTIONS)


def align_question(question: dict[str, Any], diffs: list[tuple[sqlite3.Row, str]],
                   vocab: dict[str, Any]) -> list[dict[str, Any]]:
    """Pair one question with the disclosure changes it plausibly produced."""
    terms = discriminating(f"{question['topic']}{question['question_text']}", vocab)
    floor = min(MIN_SHARED_TERMS, len(terms) // SHORT_QUESTION_SHARE)
    scored = []
    for diff, text in diffs:
        score, shared = overlap(terms, text, vocab)
        if len(shared) >= floor and score >= MIN_MATCH_SCORE:
            scored.append((score, shared, diff))
    scored.sort(key=lambda row: (-row[0], row[2]["diff_id"]))
    triplets = []
    # The same paragraph is often changed in two consecutive drafts, which is two
    # diff rows saying the same thing.  A question's list should name distinct
    # changes, so only the best-scoring row for a passage is kept.
    seen: set[str] = set()
    for score, shared, diff in scored:
        passage = (diff["right_text"] or diff["left_text"] or diff["metric"] or "")[:80]
        if passage in seen:
            continue
        seen.add(passage)
        if len(triplets) >= DIFFS_PER_QUESTION:
            break
        # Which paragraph of the reply speaks to this change: the answer is
        # cited by page, so it has to be the paragraph, not the question block.
        answers = [row for row in question["paragraphs"]
                   if question["answer_first_paragraph_id"] is not None
                   and row["paragraph_id"] >= question["answer_first_paragraph_id"]]
        changed = discriminating(diff["right_text"] or diff["left_text"] or "", vocab)
        best = max(answers or question["paragraphs"],
                   key=lambda row: (overlap(changed, row["text"], vocab)[0], -row["paragraph_id"]))
        triplets.append({
            "triplet_id": identifier("triplet", question["question_id"], diff["diff_id"]),
            "question_id": question["question_id"], "company_id": question["company_id"],
            "response_paragraph_id": best["paragraph_id"], "response_page": best["page"],
            "response_excerpt": best["text"][:400], "diff_id": diff["diff_id"],
            "diff_section": diff["section"], "diff_kind": diff["diff_kind"],
            "shared_terms": json.dumps(shared[:12], ensure_ascii=False),
            "match_score": round(score, 4), "created_at": utc_now()})
    return triplets


def drop_stale_triplets(db: sqlite3.Connection, version_id: str) -> int:
    """Clear a document's previous triplets before writing the new ones.

    Triplets point at ``disclosure_diffs`` rows, and those are rebuilt whenever
    the comparison changes.  Without this the old rows survive an upsert and go
    on citing diff ids that no longer exist, which reads as evidence right up
    until someone follows the link.
    """
    cursor = db.execute("""DELETE FROM inquiry_triplets WHERE question_id IN
        (SELECT question_id FROM inquiry_questions WHERE version_id=?)""", (version_id,))
    return cursor.rowcount


def store(db: sqlite3.Connection, questions: list[dict[str, Any]],
          triplets: list[dict[str, Any]]) -> None:
    db.executemany("""INSERT OR REPLACE INTO inquiry_questions
        (question_id,company_id,version_id,round,document_title,number,topic,question_text,
         question_page,first_paragraph_id,last_paragraph_id,answer_first_paragraph_id,
         answer_page,aligned_at)
        VALUES(:question_id,:company_id,:version_id,:round,:document_title,:number,:topic,
               :question_text,:question_page,:first_paragraph_id,:last_paragraph_id,
               :answer_first_paragraph_id,:answer_page,:aligned_at)""",
        [{key: value for key, value in question.items() if key != "paragraphs"}
         for question in questions])
    db.executemany("""INSERT OR REPLACE INTO inquiry_triplets
        (triplet_id,question_id,company_id,response_paragraph_id,response_page,response_excerpt,
         diff_id,diff_section,diff_kind,shared_terms,match_score,created_at)
        VALUES(:triplet_id,:question_id,:company_id,:response_paragraph_id,:response_page,
               :response_excerpt,:diff_id,:diff_section,:diff_kind,:shared_terms,:match_score,
               :created_at)""", triplets)
    db.commit()


def question_for_paragraph(db: sqlite3.Connection, version_id: str,
                           paragraph_id: int) -> sqlite3.Row | None:
    """The inquiry question a cited paragraph answers, if it answers one."""
    return db.execute("""SELECT * FROM inquiry_questions
        WHERE version_id=? AND ?>=first_paragraph_id AND ?<=last_paragraph_id
        ORDER BY first_paragraph_id DESC LIMIT 1""",
        (version_id, paragraph_id, paragraph_id)).fetchone()


def run(companies: set[str] | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    db = connect()
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    summary: dict[str, Any] = {"documents": 0, "questions": 0, "questions_with_answer": 0,
                               "triplets": 0, "stale_triplets_removed": 0,
                               "dry_run": dry_run, "by_document": []}
    vocabularies: dict[str, dict[str, Any]] = {}
    for document in response_documents(db, companies):
        rows = db.execute("""SELECT paragraph_id,page,section,text FROM evidence_paragraphs
            WHERE version_id=? ORDER BY paragraph_id""", (document["version_id"],)).fetchall()
        questions = parse_questions(rows, document)
        vocab = vocabularies.setdefault(document["company_id"],
                                        vocabulary(db, document["company_id"]))
        diffs = [(diff, " ".join(filter(None, (diff["section"], diff["right_text"] or "",
                                               diff["left_text"] or "", diff["metric"] or ""))))
                 for diff in db.execute("SELECT * FROM disclosure_diffs WHERE company_id=?",
                                        (document["company_id"],)).fetchall()
                 if alignable(diff)]
        triplets: list[dict[str, Any]] = []
        for question in questions:
            triplets.extend(align_question(question, diffs, vocab))
        if not dry_run:
            summary["stale_triplets_removed"] += drop_stale_triplets(db, document["version_id"])
            store(db, questions, triplets)
        summary["documents"] += 1
        summary["questions"] += len(questions)
        summary["questions_with_answer"] += sum(
            1 for question in questions if question["answer_first_paragraph_id"])
        summary["triplets"] += len(triplets)
        summary["by_document"].append({
            "title": document["title"], "round": round_of(document["title"]),
            "questions": len(questions), "triplets": len(triplets),
            "numbers": [question["number"] for question in questions]})
    db.close()
    if not dry_run:
        audit({"source_id": "system", "outcome": "inquiry_alignment_completed",
               "questions": summary["questions"], "triplets": summary["triplets"]})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Align inquiry questions to their answers and the disclosure changes they caused")
    parser.add_argument("--company", action="append", choices=configured_companies())
    parser.add_argument("--dry-run", action="store_true", help="parse and match without writing")
    args = parser.parse_args()
    print(json.dumps(run(set(args.company) if args.company else None, dry_run=args.dry_run),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
