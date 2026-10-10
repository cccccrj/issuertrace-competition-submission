#!/usr/bin/env python3
"""Does dense recall earn its place?  Recall of BM25, dense and hybrid on three gold sets.

The question is narrow on purpose.  平台深化路线 §3.2.1 showed that perfect
retrieval could rescue at most 6.5% of the decidable UNVERIFIABLE Chinese
claims, so "vectors are better" is not worth asking in general.  What is
worth asking is whether they fix the cases BM25 structurally cannot reach,
without breaking the ones it already gets right:

``cross_lingual``  An English claim whose answer sits in a Chinese paragraph,
                   or the reverse.  BM25 shares no term across the two; this is
                   the case vectors are built to win.
``numeric``        Same language, matched by distinctive figures.  The §3.2.1
                   method, kept so the two reports can be read together.
``verbatim_en``    An English claim whose wording appears verbatim in another
                   filing.  Lexical retrieval's home ground: fusion must not
                   lose it.

Gold is built without any retriever, so no method is graded on its own output:
a paragraph is gold for a claim when it shares at least two *distinctive*
figures with it (two decimals or more, five digits or more, or a
comma-grouped number -- figures that do not recur by chance), or, for
``verbatim_en``, a six-word run.  Figures survive translation, which is what
makes a cross-lingual gold set possible without a translator.  The claim's own
source document is never gold: finding the article a claim was extracted from
proves nothing.

What gold built this way cannot see is a paraphrase with no figure in it, so
the numbers here are a floor for dense recall's benefit, not an estimate of it.

    python -m processor.retrieval_eval build
    python -m processor.retrieval_eval run [--neighbors bundle/neighbors.jsonl]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from collector.collect import DATA, connect

EVAL_DIR = DATA / "state" / "retrieval_eval"
GOLD_PATH = EVAL_DIR / "gold.jsonl"
DEPTHS = (5, 20)
# The adoption rule, fixed before any vector exists so the result cannot move
# the bar.  Hybrid must add this much recall@5 on cross-lingual claims, and may
# lose at most this much on the verbatim set, where BM25 is already strong.
ADOPT_MIN_GAIN = 0.10
ADOPT_MAX_LOSS = 0.02

CJK = re.compile(r"[一-鿿]")
LATIN = re.compile(r"[A-Za-z]")
FIGURE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d{2,}|\d{5,})(?![\d,]|\.\d)")
WORD = re.compile(r"[a-z0-9]+")
SHINGLE_WORDS = 6


def language(text: str | None) -> str:
    """'en' when Latin letters outnumber Chinese characters three to one, else 'zh'."""
    text = text or ""
    cjk, latin = len(CJK.findall(text)), len(LATIN.findall(text))
    return "en" if latin > 3 * cjk and latin > 20 else "zh"


def figures(text: str | None) -> set[str]:
    """Distinctive figures, commas dropped so 20,275,407 and 20275407 agree."""
    return {match.replace(",", "") for match in FIGURE.findall(text or "")}


def shingles(text: str | None, size: int = SHINGLE_WORDS) -> set[str]:
    words = WORD.findall((text or "").lower())
    return {" ".join(words[index:index + size]) for index in range(len(words) - size + 1)}


def build_gold(db: sqlite3.Connection) -> list[dict[str, Any]]:
    paragraphs = db.execute(
        "SELECT paragraph_id,company_id,version_id,text FROM evidence_paragraphs").fetchall()
    by_figure: dict[tuple[str, str], set[int]] = defaultdict(set)
    by_shingle: dict[tuple[str, str], set[int]] = defaultdict(set)
    meta: dict[int, tuple[str, str]] = {}
    for paragraph_id, company_id, version_id, text in paragraphs:
        lang = language(text)
        meta[paragraph_id] = (version_id, lang)
        for figure in figures(text):
            by_figure[(company_id, figure)].add(paragraph_id)
        if lang == "en":
            for shingle in shingles(text):
                by_shingle[(company_id, shingle)].add(paragraph_id)

    claims = db.execute("""SELECT c.claim_id,c.text,e.company_id,e.version_id,v.verdict
        FROM event_claims c JOIN event_records e ON e.event_id=c.event_id
        LEFT JOIN claim_verdicts v ON v.claim_id=c.claim_id
        WHERE e.company_id IS NOT NULL ORDER BY c.claim_id""").fetchall()
    gold: list[dict[str, Any]] = []
    for claim_id, text, company_id, source_version, verdict in claims:
        lang = language(text)
        base = {"claim_id": claim_id, "company": company_id, "language": lang, "verdict": verdict}
        hits: dict[int, int] = defaultdict(int)
        for figure in figures(text):
            for paragraph_id in by_figure.get((company_id, figure), ()):
                hits[paragraph_id] += 1
        matched = [pid for pid, count in hits.items() if count >= 2 and meta[pid][0] != source_version]
        crossed = sorted(pid for pid in matched if meta[pid][1] != lang)
        same = sorted(pid for pid in matched if meta[pid][1] == lang)
        if crossed:
            gold.append({**base, "set": "cross_lingual", "gold": crossed})
        if same:
            gold.append({**base, "set": "numeric", "gold": same})
        if lang == "en":
            verbatim: set[int] = set()
            for shingle in shingles(text):
                verbatim |= by_shingle.get((company_id, shingle), set())
            verbatim = {pid for pid in verbatim if meta[pid][0] != source_version}
            if verbatim:
                gold.append({**base, "set": "verbatim_en", "gold": sorted(verbatim)})
    return gold


def write_gold(rows: Iterable[dict[str, Any]], path: Path = GOLD_PATH) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = defaultdict(int)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            counts[row["set"]] += 1
    return dict(counts)


def read_neighbors(path: Path) -> dict[str, list[int]]:
    neighbors = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                neighbors[row["id"]] = [int(hit[0]) for hit in row["hits"]]
    return neighbors


def score(ranked: list[int], gold: set[int], shown: list[int] | None = None) -> dict[str, float]:
    """recall@k (any gold paragraph within the first k), reciprocal rank within 20,
    and ``recall@shown``: whether the candidates the adjudicator actually reads
    contain gold -- five for BM25 and hybrid, up to seven for fallback."""
    out = {f"recall@{depth}": float(bool(gold & set(ranked[:depth]))) for depth in DEPTHS}
    out["recall@shown"] = float(bool(gold & set(shown if shown is not None else ranked[:5])))
    first = next((rank for rank, pid in enumerate(ranked[:max(DEPTHS)], start=1) if pid in gold), None)
    out["mrr@20"] = 1.0 / first if first else 0.0
    return out


def evaluate(db: sqlite3.Connection, gold_rows: list[dict[str, Any]],
             neighbors: dict[str, list[int]] | None) -> dict[str, Any]:
    from processor.claim_index import search
    from processor.dense_index import rrf
    texts = dict(db.execute("SELECT claim_id,text FROM event_claims").fetchall())
    from processor.dense_index import FALLBACK_EXTRA
    methods = ["bm25"] + (["dense", "hybrid", "fallback"] if neighbors is not None else [])
    # ``any_language``: one row per claim with every gold paragraph from every
    # set.  It is the question adjudication actually asks -- is there *some*
    # evidence in front of the model -- and the one the 2026-10-04 adoption
    # rule should have been written against: 184 of the 195 cross-lingual
    # claims also had same-language evidence, so "find the other-language twin"
    # measured something adjudication never needed.
    merged: dict[str, dict[str, Any]] = {}
    for row in gold_rows:
        entry = merged.setdefault(row["claim_id"], {**row, "set": "any_language", "gold": []})
        entry["gold"] = sorted(set(entry["gold"]) | set(row["gold"]))
    gold_rows = list(gold_rows) + list(merged.values())
    totals: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[tuple[str, str], int] = defaultdict(int)
    covered: dict[str, int] = defaultdict(int)
    cache: dict[tuple[str, str], list[int]] = {}
    for row in gold_rows:
        if row["claim_id"] not in texts:
            continue
        key = (row["company"], texts[row["claim_id"]])
        if key not in cache:
            cache[key] = [hit["paragraph_id"] for hit in search(db, row["company"], key[1], max(DEPTHS))]
        ranked = {"bm25": cache[key]}
        if neighbors is not None:
            dense = neighbors.get(row["claim_id"])
            if dense is None:
                continue  # not embedded: leave it out of every column, not just the dense ones
            covered[row["set"]] += 1
            ranked["dense"] = dense[:max(DEPTHS)]
            ranked["hybrid"] = rrf(cache[key], dense[:max(DEPTHS)])
            base = cache[key][:5]
            ranked["fallback"] = base + [pid for pid in dense if pid not in base][:FALLBACK_EXTRA]
        gold = set(row["gold"])
        for subset in ("all", "unverifiable") if row.get("verdict") == "UNVERIFIABLE" else ("all",):
            counts[(row["set"], subset)] += 1
            for method in methods:
                shown = ranked[method] if method == "fallback" else ranked[method][:5]
                for metric, value in score(ranked[method], gold, shown).items():
                    totals[(row["set"], subset, method)][metric] += value
    table: dict[str, Any] = {}
    for (set_name, subset), n in sorted(counts.items()):
        table.setdefault(set_name, {})[subset] = {
            "claims": n,
            **{method: {metric: round(value / n, 4) for metric, value in totals[(set_name, subset, method)].items()}
               for method in methods}}
    return {"methods": methods, "sets": table, "decision": decide(table) if neighbors is not None else None}


def decide(table: dict[str, Any]) -> dict[str, Any]:
    """Apply the adoption rule fixed in ADOPT_MIN_GAIN / ADOPT_MAX_LOSS."""
    def r5(set_name: str, method: str) -> float | None:
        cell = table.get(set_name, {}).get("all", {}).get(method)
        return cell["recall@5"] if cell else None
    gain = (r5("cross_lingual", "hybrid") or 0.0) - (r5("cross_lingual", "bm25") or 0.0)
    loss = (r5("verbatim_en", "bm25") or 0.0) - (r5("verbatim_en", "hybrid") or 0.0)
    adopt = gain >= ADOPT_MIN_GAIN and loss <= ADOPT_MAX_LOSS
    return {"cross_lingual_gain@5": round(gain, 4), "verbatim_loss@5": round(loss, 4),
            "rule": f"gain ≥ {ADOPT_MIN_GAIN:.2f} and loss ≤ {ADOPT_MAX_LOSS:.2f}",
            "adopt_hybrid": adopt}


def render(report: dict[str, Any]) -> str:
    lines = []
    for set_name, subsets in report["sets"].items():
        for subset, cell in subsets.items():
            parts = [f"{method} shown {cell[method]['recall@shown']:.3f} R@5 {cell[method]['recall@5']:.3f} "
                     f"R@20 {cell[method]['recall@20']:.3f}" for method in report["methods"]]
            lines.append(f"{set_name:14s} {subset:12s} n={cell['claims']:5d} | " + " | ".join(parts))
    if report.get("decision"):
        lines.append("decision: " + json.dumps(report["decision"], ensure_ascii=False))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="derive the gold sets from the database (no retriever involved)")
    run = sub.add_parser("run", help="score BM25 (and dense / hybrid when neighbours are given)")
    run.add_argument("--neighbors", type=Path,
                     help="neighbors.jsonl from the GPU server (default: the installed one, if any)")
    run.add_argument("--out", type=Path, help="also write the report as JSON here")
    args = parser.parse_args(argv)
    db = connect()
    db.row_factory = sqlite3.Row  # claim_index.search reads columns by name
    try:
        if args.command == "build":
            print(json.dumps(write_gold(build_gold(db)), ensure_ascii=False, indent=2))
            return 0
        gold_rows = [json.loads(line) for line in GOLD_PATH.read_text(encoding="utf-8").splitlines() if line]
        neighbors = None
        if args.neighbors:
            neighbors = read_neighbors(args.neighbors)
        else:
            from processor.dense_index import load
            neighbors = load(db)
        report = evaluate(db, gold_rows, neighbors)
        print(render(report))
        if args.out:
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
