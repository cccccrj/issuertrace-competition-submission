#!/usr/bin/env python3
"""Retrieve labelled gold articles to show a prompt what the task looks like.

E1 asks the model to place an article into a 26-code issue vocabulary and gets
test micro-F1 0.4211.  The prompt is 3,197 characters of hand-written criteria
and **not one example**, while the organiser's train split carries 2,081 articles
a human has already labelled.  Those labels have always been usable -- train is
for development -- so this is not something a rule was blocking; it simply was
never done.

What the criteria cannot convey is where one code stops and its neighbour
starts.  「进入原本没有的地盘」 is true of building a line, shipping a technology
and changing a strategy at once, which is why BUSINESS_EXPANSION alone absorbed
105 misjudgements on train (see ``issue_codes`` v2's note).  A worked example
settles that boundary in a way a sentence about it does not.

Retrieval rather than a fixed example set: the vocabulary is long-tailed
(TECH_LEVEL 592 rows against FINANCIAL's thirteen), so a fixed sample either
misses the rare codes or misrepresents the common ones.  Nearest neighbours by
BM25 put the codes that actually compete for *this* article in front of the
model.

Three invariants live in the code rather than in a reviewer's memory, because
each of them silently invalidates every number the harness produces:

  1. **The pool is train, always.**  ``rebuild`` takes no split; ``TRAIN_SPLIT``
     is a module constant and the built index records which split it holds, so a
     test can assert it rather than trust it.
  2. **An article never retrieves itself.**  Judging train would otherwise be
     copying the answer, and the k sweep would pick the largest k every time.
  3. **k=0 changes nothing.**  ``examples_for`` returns an empty list and the
     caller's prompt is byte-identical to the zero-shot one, so "off means off"
     is demonstrable.

The index is FTS5 over CJK bigrams, the same shape ``processor.claim_index`` has
been serving retrieval from; ``bigrams`` and ``prune_tokens`` are imported from
there rather than copied.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any

from processor.claim_index import bigrams, prune_tokens

# The pool split, fixed here and nowhere else.  A parameter would be one
# distracted afternoon away from being passed "test".
TRAIN_SPLIT = "train"
# Enough of a piece to show why it carries the code it carries.  Far shorter
# than the 1,800 characters the article under judgement gets: eight examples at
# this length cost less prompt than one more article would.
EXAMPLE_CHARS = 220
# Retrieval only has to be good enough to put the competing codes in front of
# the model; ranking beyond this is spent on articles the prompt will not hold.
SEARCH_POOL = 40
# The query is the headline and the lead, not the whole article.  1,800
# characters tokenize into some 900 bigrams, and what an article is *about* is
# settled in its first paragraph; the tail adds terms that dilute the ranking.
QUERY_CHARS = 300
# Against 2,046 documents the query does not need to narrow, it needs to rank,
# and BM25 already discounts a common term by its IDF.  ``claim_index`` keeps
# twenty because it merges posting lists over 48k paragraphs; here that left a
# quarter of the articles with no candidate at all.
QUERY_TERM_LIMIT = 120


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS gold_examples(
        example_id INTEGER PRIMARY KEY,
        article_file_id TEXT NOT NULL UNIQUE, split TEXT NOT NULL, sheet TEXT NOT NULL,
        title TEXT, body TEXT NOT NULL,
        event_name TEXT NOT NULL, event_emotion TEXT, issue_codes_json TEXT NOT NULL)""")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS gold_examples_fts USING fts5(body, tokenize='unicode61')")
    db.execute("""CREATE TABLE IF NOT EXISTS gold_example_terms(
        token TEXT PRIMARY KEY, documents INTEGER NOT NULL)""")


def rebuild(db: sqlite3.Connection, taxonomy: dict[str, Any]) -> dict[str, Any]:
    """Index the labelled train articles.  Takes no split on purpose (invariant 1)."""
    from processor.benchmark import labelled_documents, mapped_codes

    ensure_schema(db)
    db.execute("DELETE FROM gold_examples")
    db.execute("DELETE FROM gold_examples_fts")
    db.execute("DELETE FROM gold_example_terms")
    frequency: dict[str, int] = {}
    indexed = 0
    for doc, text, row in labelled_documents(db, TRAIN_SPLIT):
        codes = mapped_codes(taxonomy, "issue_codes", row["sheet"], row["event_name"])
        body = (text or "").strip()[:EXAMPLE_CHARS]
        if not body:
            continue
        cursor = db.execute("""INSERT INTO gold_examples
            (article_file_id,split,sheet,title,body,event_name,event_emotion,issue_codes_json)
            VALUES(?,?,?,?,?,?,?,?)""",
            (row["article_file_id"], TRAIN_SPLIT, row["sheet"], row["title"], body,
             row["event_name"], row["event_emotion"], json.dumps(sorted(codes), ensure_ascii=False)))
        tokens = searchable(f"{row['title'] or ''}\n{body}")
        db.execute("INSERT INTO gold_examples_fts(rowid, body) VALUES(?,?)",
                   (cursor.lastrowid, " ".join(tokens)))
        for token in set(tokens):
            frequency[token] = frequency.get(token, 0) + 1
        indexed += 1
    db.executemany("INSERT INTO gold_example_terms(token,documents) VALUES(?,?)",
                   sorted(frequency.items()))
    db.commit()
    return {"split": TRAIN_SPLIT, "indexed": indexed, "terms": len(frequency)}


def searchable(text: str) -> list[str]:
    """The tokens that survive into the FTS index, used for indexing *and* querying.

    ``bigrams`` emits Latin tokens that may carry ``.`` and ``%`` (``17.26%``),
    but the index is built by handing that output to FTS5's ``unicode61``, which
    splits on exactly those characters and stores ``17`` and ``26`` instead.  A
    term table recording the pre-split form therefore lists terms the index does
    not contain -- and because they are the rarest terms in any article, the
    rarest-first selection filled every slot with them and matched nothing.
    Retrieval returned empty for one row in four before this filter existed.

    Filtering both sides through one function is what keeps the two in step; a
    comment asking future callers to remember would not have.
    """
    return [token for token in bigrams(text) if token.isalnum()]


def query_text(title: str | None, text: str | None) -> str:
    """The retrieval query, built the same way for every task that asks."""
    return f"{title or ''}\n{(text or '')[:QUERY_CHARS]}"


def indexed_splits(db: sqlite3.Connection) -> set[str]:
    """Which splits the built index actually holds — assertable, not assumed."""
    return {row[0] for row in db.execute("SELECT DISTINCT split FROM gold_examples")}


def examples_for(db: sqlite3.Connection, query: str, k: int, field: str,
                 exclude_article_id: str | None = None) -> list[dict[str, Any]]:
    """The k nearest labelled train articles that can answer ``field``, nearest first.

    ``field`` is ``issue_codes`` or ``event_emotion``.  An article the frozen map
    sends to the unmappable bucket -- 「公共卫生事件」, 32 rows -- carries no issue
    code and so demonstrates nothing for E1, while still being a perfectly good
    E3 example.  Filtering here rather than in the caller keeps the set that is
    searched and the set that is rendered the same set.

    ``exclude_article_id`` is invariant 2 and the caller must pass it whenever
    the article being judged could itself be in the pool.  ``k <= 0`` is
    invariant 3: no query runs and no example is returned.
    """
    if k <= 0:
        return []
    tokens = list(dict.fromkeys(searchable(query)))
    if not tokens:
        return []
    placeholders = ",".join("?" for _ in tokens)
    frequency = dict(db.execute(
        f"SELECT token, documents FROM gold_example_terms WHERE token IN ({placeholders})",
        tokens).fetchall())
    # Only terms the pool actually contains.  ``prune_tokens`` ranks rarest
    # first and treats an unknown term as frequency zero, which is right for a
    # short claim but wrong here: a 300-character query still yields terms
    # unique to this article, and they would fill every one of the twenty slots
    # with words that match nothing -- retrieval returned empty for every row
    # before this line existed.
    present = [token for token in tokens if token in frequency]
    total = db.execute("SELECT COUNT(*) FROM gold_examples").fetchone()[0] or 1
    pruned = prune_tokens(present, frequency, total, QUERY_TERM_LIMIT)
    if not pruned:
        return []
    expression = " OR ".join(f'"{token}"' for token in pruned)
    rows = db.execute("""SELECT e.article_file_id, e.title, e.body, e.event_name,
                                e.event_emotion, e.issue_codes_json, bm25(gold_examples_fts) AS score
        FROM gold_examples_fts JOIN gold_examples e ON e.example_id = gold_examples_fts.rowid
        WHERE gold_examples_fts MATCH ? ORDER BY score LIMIT ?""",
        (expression, SEARCH_POOL)).fetchall()
    out = []
    for row in rows:
        if exclude_article_id is not None and row["article_file_id"] == exclude_article_id:
            continue
        example = {"article_file_id": row["article_file_id"], "title": row["title"],
                   "body": row["body"], "event_name": row["event_name"],
                   "event_emotion": row["event_emotion"],
                   "issue_codes": json.loads(row["issue_codes_json"]),
                   "score": float(row["score"])}
        if not example.get(field):
            continue
        out.append(example)
        if len(out) == k:
            break
    return out


def render(examples: list[dict[str, Any]], field: str) -> str:
    """The example block, in the shape both prompts embed.

    ``field`` names the answer being demonstrated -- ``issue_codes`` for E1,
    ``event_emotion`` for E3 -- so one renderer serves both and the two prompts
    cannot drift into showing examples differently.

    No reason or rationale is rendered: the gold carries none, and inventing one
    would teach the model to imitate a justification nobody wrote.
    """
    blocks = []
    for index, example in enumerate(examples, 1):
        answer = example[field]
        shown = "、".join(answer) if isinstance(answer, list) else str(answer)
        blocks.append(f"[示例{index}] 标题：{example['title'] or '（无）'}\n"
                      f"正文：{example['body']}\n答案：{shown}")
    return "\n\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    from processor.benchmark import BENCHMARK_DB, connect, load_taxonomy_map

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--query", help="try a retrieval against the built index")
    parser.add_argument("--k", type=int, default=4)
    args = parser.parse_args(argv)
    db = connect(BENCHMARK_DB)
    if args.rebuild:
        print(json.dumps(rebuild(db, load_taxonomy_map()), ensure_ascii=False))
    if args.query:
        for example in examples_for(db, args.query, args.k):
            print(f"  {example['score']:.2f}  {example['issue_codes']}  {example['title']}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
