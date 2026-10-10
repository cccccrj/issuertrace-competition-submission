#!/usr/bin/env python3
"""Full-text index over authoritative disclosure, used to find evidence for a claim.

Retrieval has to work on paraphrase: a media claim almost never repeats a filing
verbatim.  FTS5's ``trigram`` tokenizer only matches substrings, so it finds
nothing for a rewritten sentence.  Chinese character bigrams indexed through
``unicode61`` and ranked by BM25 do work, and stay inside the standard library.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from typing import Any

from zhconv import convert

from collector.collect import ROOT, audit, connect
from processor.disclosure_diff import load_versions, paragraphs
from processor.pipeline import configured_companies, utc_now

# Filings a claim can be checked against, in two shapes.  A chain type is one
# document filed repeatedly as drafts, so only its newest version is worth
# indexing.  A collection type is many distinct documents, where dropping any
# one of them drops the only page that can answer some claim.
CHAIN_DOCUMENT_TYPES = ("prospectus",)
COLLECTION_DOCUMENT_TYPES = ("review_responses", "announcements", "attachment")
EVIDENCE_DOCUMENT_TYPES = CHAIN_DOCUMENT_TYPES + COLLECTION_DOCUMENT_TYPES
# Allocation results, lottery numbers and proxy forms are excluded on purpose:
# they are tables of investor names and ticket numbers, which match many bigrams
# while supporting no claim.
EXCLUDED_TITLE_MARKERS = ("代表委任表格", "form of proxy", "中签号码", "中籤號碼",
                          "配售结果", "配售結果")
MIN_PARAGRAPH_CHARS = 20
CJK = re.compile(r"[一-鿿]")
TOKEN = re.compile(r"[一-鿿]+|[A-Za-z0-9][A-Za-z0-9.%]*")
WORD_BOUNDARY = re.compile(r"(?<=[A-Za-z])\s+(?=[A-Za-z0-9])|(?<=[0-9])\s+(?=[A-Za-z])")
# A query is only as selective as its rarest terms.  One claim tokenizes into 80
# or more bigrams, and OR-ing all of them makes FTS merge a posting list for
# every one — including 公司 and 股份, which appear almost everywhere and shift
# no BM25 ranking.  Measured on the 48k-paragraph index, the unpruned query took
# 12–136 seconds per claim; keeping only the rarest terms answers in
# milliseconds and, if anything, ranks better.
QUERY_TERM_LIMIT = 20
COMMON_TERM_SHARE = 0.08


def fold(text: str) -> str:
    """Fold traditional characters to simplified before tokenizing.

    Half of the WuXi AppTec corpus is Hong Kong originals written in traditional
    Chinese, while every claim is written in simplified.  Without this fold,
    藥明康德 shares no bigram with 药明康德 and the whole H-share corpus is
    unreachable no matter how relevant it is.  Only the search body is folded;
    ``evidence_paragraphs.text`` keeps the document's own wording, because a
    citation has to quote what the filing actually says.
    """
    return convert(text, "zh-cn")


def join_broken_runs(text: str) -> str:
    """Delete the whitespace PDF extraction inserts, but keep the spaces between words.

    Extraction breaks Chinese runs and long numbers with stray spaces (投资 总额,
    1 234), and those must be glued back.  Deleting *every* space, which this
    used to do, also glued English into one token per clause: "WuXi AppTec
    announced revenue" indexed as ``wuxiapptecannouncedrevenue`` and matched
    nothing but itself.  Measured on 842 English WuXi claims whose wording
    appears verbatim in another filing, the source paragraph reached the top
    five for 68% of them before this split and 94% after.

    A space survives only where it separates two Latin/digit characters and at
    least one of them is a letter: that is a word boundary.  Digit-to-digit and
    anything touching a Chinese character is still extraction noise.
    """
    text = WORD_BOUNDARY.sub("\x00", text)
    return re.sub(r"\s+", "", text)


def bigrams(text: str) -> list[str]:
    """Tokenize mixed Chinese/Latin text into overlapping CJK bigrams and words.

    Chinese has no spaces, so ``unicode61`` would index a whole run of characters
    as one token and never match a paraphrase.  Overlapping bigrams give partial
    matches the weight they deserve while keeping the index small.
    """
    tokens: list[str] = []
    for match in TOKEN.finditer(join_broken_runs(fold(text))):
        token = match.group()
        if CJK.match(token):
            tokens.extend([token[index:index + 2] for index in range(len(token) - 1)] or [token])
        else:
            tokens.append(token.lower())
    return tokens


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS evidence_paragraphs(
        paragraph_id INTEGER PRIMARY KEY,
        company_id TEXT NOT NULL, version_id TEXT NOT NULL, document_type TEXT,
        title TEXT, section TEXT, page INTEGER, text TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS evidence_paragraphs_company ON evidence_paragraphs(company_id)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(body, tokenize='unicode61')")
    # How many paragraphs each term appears in, so a query can drop the terms
    # that would only cost time.  Counted per company because that is the scope
    # every search runs in.
    db.execute("""CREATE TABLE IF NOT EXISTS evidence_terms(
        company_id TEXT NOT NULL, token TEXT NOT NULL, documents INTEGER NOT NULL,
        PRIMARY KEY(company_id, token))""")


def indexable(version: sqlite3.Row) -> bool:
    """Whether a filing can carry evidence at all."""
    title = str(version["title"] or "").lower()
    return not any(marker in title for marker in EXCLUDED_TITLE_MARKERS)


def evidence_versions(db: sqlite3.Connection, company_id: str) -> list[tuple[str, sqlite3.Row]]:
    """Pick the filings worth indexing, paired with their document type.

    Only the newest prospectus is indexed.  The three drafts are near-identical,
    so indexing all of them makes every search return the same paragraph three
    times and pushes genuinely different evidence off the result list; which
    draft a passage changed in is a question ``disclosure_diffs`` already answers.
    A collection type gets the opposite treatment: each announcement states
    different facts on a different date, so keeping only the newest would throw
    away the page every earlier claim needs.
    """
    selected: list[tuple[str, sqlite3.Row]] = []
    for document_type in EVIDENCE_DOCUMENT_TYPES:
        # load_versions() drops HTML-only mirrors (no page anchors) and returns
        # the chain in regulatory order, so the last entry is the current filing.
        versions, _ = load_versions(db, company_id, document_type, paginated_only=True)
        versions = [version for version in versions if indexable(version)]
        if not versions:
            continue
        kept = versions[-1:] if document_type in CHAIN_DOCUMENT_TYPES else versions
        selected.extend((document_type, version) for version in kept)
    return selected


def rebuild(companies: set[str] | None = None) -> dict[str, Any]:
    """Rebuild the whole index.

    No incremental path: the corpus is small enough that a full rebuild is
    simpler than tracking what changed, and correctness is worth more here than
    latency.  It is not, however, fast any more — 93,209 paragraphs and their
    bigrams take about twenty minutes, not the "under a second" this said when
    the index held a fraction of that.  Budget for it before running it against
    a live database.
    """
    db = connect()
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    targets = sorted(companies) if companies else [
        row[0] for row in db.execute("SELECT DISTINCT company_id FROM document_versions WHERE company_id IS NOT NULL")]
    summary: dict[str, Any] = {"companies": [], "paragraphs": 0}
    for company_id in targets:
        versions = evidence_versions(db, company_id)
        stale = [row[0] for row in db.execute(
            "SELECT paragraph_id FROM evidence_paragraphs WHERE company_id=?", (company_id,))]
        db.executemany("DELETE FROM evidence_fts WHERE rowid=?", [(item,) for item in stale])
        db.execute("DELETE FROM evidence_paragraphs WHERE company_id=?", (company_id,))
        db.execute("DELETE FROM evidence_terms WHERE company_id=?", (company_id,))
        written = 0
        frequencies: Counter[str] = Counter()
        for document_type, version in versions:
            text = (ROOT / version["text_path"]).read_text(encoding="utf-8")
            for para in paragraphs(text):
                if len(para["text"]) < MIN_PARAGRAPH_CHARS:
                    continue
                cursor = db.execute("""INSERT INTO evidence_paragraphs
                    (company_id,version_id,document_type,title,section,page,text) VALUES(?,?,?,?,?,?,?)""",
                    (company_id, version["version_id"], document_type,
                     version["title"], para["section"], para["page"], para["text"]))
                tokens = bigrams(para["text"])
                db.execute("INSERT INTO evidence_fts(rowid,body) VALUES(?,?)",
                           (cursor.lastrowid, " ".join(tokens)))
                # Document frequency, so a term repeated inside one paragraph
                # still counts once.
                frequencies.update(set(tokens))
                written += 1
        db.executemany("INSERT INTO evidence_terms(company_id,token,documents) VALUES(?,?,?)",
                       [(company_id, token, count) for token, count in frequencies.items()])
        summary["companies"].append({"company": company_id, "documents": len(versions), "paragraphs": written})
        summary["paragraphs"] += written
        db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "claim_index_rebuilt",
           "paragraphs": summary["paragraphs"], "at": utc_now()})
    return summary


def retokenize_into(db: sqlite3.Connection, companies: set[str] | None = None) -> dict[str, Any]:
    """Rewrite the FTS bodies and term counts from the stored paragraphs.

    For a tokenizer change, ``rebuild`` is the wrong tool: it re-reads every
    filing and hands out fresh paragraph ids, and those ids are what a dense
    vector file (``processor.dense_index``) is keyed by.  The paragraphs
    themselves have not changed, only how they are cut into terms, so this
    keeps every row and id and replaces nothing but the index.
    """
    targets = sorted(companies) if companies else [
        row[0] for row in db.execute("SELECT DISTINCT company_id FROM evidence_paragraphs")]
    summary: dict[str, Any] = {"companies": [], "paragraphs": 0}
    for company_id in targets:
        rows = db.execute("SELECT paragraph_id,text FROM evidence_paragraphs WHERE company_id=?",
                          (company_id,)).fetchall()
        frequencies: Counter[str] = Counter()
        for paragraph_id, text in rows:
            tokens = bigrams(text)
            db.execute("DELETE FROM evidence_fts WHERE rowid=?", (paragraph_id,))
            db.execute("INSERT INTO evidence_fts(rowid,body) VALUES(?,?)", (paragraph_id, " ".join(tokens)))
            frequencies.update(set(tokens))
        db.execute("DELETE FROM evidence_terms WHERE company_id=?", (company_id,))
        db.executemany("INSERT INTO evidence_terms(company_id,token,documents) VALUES(?,?,?)",
                       [(company_id, token, count) for token, count in frequencies.items()])
        db.commit()
        summary["companies"].append({"company": company_id, "paragraphs": len(rows)})
        summary["paragraphs"] += len(rows)
    return summary


def retokenize(companies: set[str] | None = None) -> dict[str, Any]:
    db = connect()
    ensure_schema(db)
    summary = retokenize_into(db, companies)
    db.close()
    audit({"source_id": "system", "outcome": "claim_index_retokenized",
           "paragraphs": summary["paragraphs"], "at": utc_now()})
    return summary


def prune_tokens(tokens: list[str], frequency: dict[str, int], total: int,
                 limit: int = QUERY_TERM_LIMIT) -> list[str]:
    """Keep the terms that actually narrow a search, rarest first.

    Pure, and separate from the SQL that feeds it, because a second index wants
    exactly this rule over a different pair of tables -- ``processor.gold_examples``
    scopes by nothing where this module scopes by company.  Writing the rule
    twice is how ``issue_code_rules`` came to have two drifting copies.

    ``limit`` is a parameter for the same reason.  Twenty terms is right when the
    job is to *narrow* 48k paragraphs; against a 2,046-document example pool the
    job is to *rank*, and twenty of the rarest terms describe so small a union
    that a quarter of the articles matched nothing at all.

    Terms carrying no document frequency are kept rather than dropped: either
    the index has no frequency table yet, or the term appears nowhere and costs
    nothing.  Dropping them on the first reading would silently disable search.
    """
    unique = list(dict.fromkeys(tokens))
    if not unique:
        return []
    ranked = sorted(unique, key=lambda token: (frequency.get(token, 0), token))
    ceiling = (total or 1) * COMMON_TERM_SHARE
    selective = [token for token in ranked if frequency.get(token, 0) <= ceiling]
    # If every term is a common one, the query still has to run on something:
    # the rarest of them is the best available signal.
    return (selective or ranked)[:limit]


def selective_tokens(db: sqlite3.Connection, company_id: str, tokens: list[str]) -> list[str]:
    """``prune_tokens`` against this module's own index."""
    unique = list(dict.fromkeys(tokens))
    if not unique:
        return []
    placeholders = ",".join("?" for _ in unique)
    frequency = dict(db.execute(
        f"SELECT token, documents FROM evidence_terms WHERE company_id=? AND token IN ({placeholders})",
        [company_id, *unique]).fetchall()) if table_exists(db, "evidence_terms") else {}
    total = db.execute("SELECT count(*) FROM evidence_paragraphs WHERE company_id=?",
                       (company_id,)).fetchone()[0] or 1
    return prune_tokens(unique, frequency, total)


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                           (name,)).fetchone())


def search(db: sqlite3.Connection, company_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return the best-matching evidence paragraphs, best first.

    The score bounds the candidate set; it must not decide the verdict on its
    own.  A claim contradicted by a filing scores much like one the filing never
    mentions, so calling a low score "unverifiable" hides real conflicts.
    """
    tokens = selective_tokens(db, company_id, bigrams(query))
    if not tokens:
        return []
    expression = " OR ".join(f'"{token}"' for token in tokens)
    rows = db.execute("""SELECT p.paragraph_id,p.version_id,p.title,p.section,p.page,p.text,
                                bm25(evidence_fts) AS score
        FROM evidence_fts JOIN evidence_paragraphs p ON p.paragraph_id=evidence_fts.rowid
        WHERE evidence_fts MATCH ? AND p.company_id=? ORDER BY score LIMIT ?""",
        (expression, company_id, limit)).fetchall()
    return [{"paragraph_id": row["paragraph_id"], "version_id": row["version_id"], "title": row["title"],
             "section": row["section"], "page": row["page"], "text": row["text"], "score": float(row["score"])}
            for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild or query the disclosure evidence index")
    parser.add_argument("--company", action="append", choices=configured_companies())
    parser.add_argument("--query", help="run a search instead of rebuilding")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--retokenize", action="store_true",
                        help="re-cut the stored paragraphs into terms, keeping every paragraph id")
    args = parser.parse_args()
    if args.retokenize:
        print(json.dumps(retokenize(set(args.company) if args.company else None), ensure_ascii=False, indent=2))
        return
    if args.query:
        db = connect()
        db.row_factory = sqlite3.Row
        company = (args.company or [configured_companies()[0]])[0]
        print(json.dumps(search(db, company, args.query, args.limit), ensure_ascii=False, indent=2))
        db.close()
        return
    print(json.dumps(rebuild(set(args.company) if args.company else None), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
