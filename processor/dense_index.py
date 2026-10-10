#!/usr/bin/env python3
"""Dense (vector) recall over the evidence paragraphs, fused with BM25.

Why the vectors are computed elsewhere.  An embedding model wants a GPU and
torch; the platform is a SQLite file and a standard-library server, and the
development machine has neither.  So the expensive half -- embedding 93k
paragraphs and every claim, and finding each claim's nearest paragraphs -- runs
once on the GPU server through the standalone ``scripts/gpu/embed_neighbors.py``,
and what comes back is small and inert: for each claim, the ids of its 20
nearest paragraphs.  Everything on this side is plain Python.

    python -m processor.dense_index export --out bundle/     # here
    python scripts/gpu/embed_neighbors.py --bundle bundle/   # GPU server
    python -m processor.dense_index import --bundle bundle/  # here
    python -m processor.dense_index status

What the vectors are for.  BM25 over bigrams and words already finds a claim
whose wording overlaps its evidence.  What it cannot do is cross languages --
an English wire story checked against a Chinese announcement shares no term
with it -- or match a paraphrase that shares no rare word.  Measured before
this existed (平台深化路线 §3.2.1), paraphrase within Chinese caps out at 6.5%
of the decidable UNVERIFIABLE claims; the cross-language case is the one this
is built to test.  ``processor.retrieval_eval`` decides whether it earned its
place, and ``claim_verdict --retrieval hybrid`` is how it is used once it has.

Paragraph ids are the join key, so the bundle records a fingerprint of every
(id, text) pair it was built from.  ``import`` refuses a bundle whose
fingerprint no longer matches the database (an index ``rebuild`` renumbers
paragraphs; ``claim_index --retokenize`` does not), and ``neighbors`` returns
nothing for a stale install rather than pointing at the wrong paragraphs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from collector.collect import DATA, connect

INSTALL_DIR = DATA / "state" / "dense"
# Reciprocal-rank fusion constant.  60 is the value from the original RRF paper
# and the common default; it flattens the gap between rank 1 and rank 5 so that
# one list cannot drown out the other.  Not tuned: tuning it on the evaluation
# sets would leave no honest number to report.
RRF_K = 60
# Each list contributes this many candidates to the fusion; the fused list is
# then cut to the caller's limit (five for adjudication).
FUSION_DEPTH = 20
# Fallback mode appends at most this many dense-only paragraphs after BM25's
# five.  Fixed at two before the effect was measured (2026-10-04); measured
# afterwards it was not changed -- one more slot bought one more hit.
FALLBACK_EXTRA = 2


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def paragraph_fingerprint(db: sqlite3.Connection) -> str:
    """One hash over every (paragraph id, text): changes if any id is renumbered or any text edited."""
    digest = hashlib.sha256()
    for paragraph_id, text in db.execute("SELECT paragraph_id,text FROM evidence_paragraphs ORDER BY paragraph_id"):
        digest.update(f"{paragraph_id}\t{hashlib.sha256(text.encode()).hexdigest()}\n".encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------

def export(out: Path, db: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Write the paragraphs and claims the GPU server needs, and nothing else.

    No database, no raw files, no keys: two JSONL files of id, company and
    text.  Paragraph text is exported as filed (traditional characters
    included) because the model reads both scripts; claims as extracted.
    """
    own = db is None
    db = db or connect()
    out.mkdir(parents=True, exist_ok=True)
    counts = {"paragraphs": 0, "queries": 0}
    with (out / "paragraphs.jsonl").open("w", encoding="utf-8") as handle:
        for paragraph_id, company_id, text in db.execute(
                "SELECT paragraph_id,company_id,text FROM evidence_paragraphs ORDER BY paragraph_id"):
            handle.write(json.dumps({"id": paragraph_id, "company": company_id, "text": text},
                                    ensure_ascii=False) + "\n")
            counts["paragraphs"] += 1
    with (out / "queries.jsonl").open("w", encoding="utf-8") as handle:
        for claim_id, company_id, text in db.execute(
                """SELECT c.claim_id,e.company_id,c.text FROM event_claims c
                   JOIN event_records e ON e.event_id=c.event_id
                   WHERE e.company_id IS NOT NULL ORDER BY c.claim_id"""):
            handle.write(json.dumps({"id": claim_id, "company": company_id, "text": text},
                                    ensure_ascii=False) + "\n")
            counts["queries"] += 1
    summary = {**counts,
               "paragraph_fingerprint": paragraph_fingerprint(db),
               "sha256": {name: sha256_file(out / name) for name in ("paragraphs.jsonl", "queries.jsonl")}}
    (out / "export.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if own:
        db.close()
    return summary


def install(bundle: Path, db: sqlite3.Connection | None = None, target: Path = INSTALL_DIR,
            allow_fake: bool = False) -> dict[str, Any]:
    """Validate a bundle that came back from the GPU server and install its neighbours."""
    own = db is None
    db = db or connect()
    try:
        export_info = json.loads((bundle / "export.json").read_text(encoding="utf-8"))
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        problems = []
        if manifest.get("input_sha256") != export_info.get("sha256"):
            problems.append("manifest.json 记录的输入哈希与 export.json 不一致：GPU 端用的不是这一份导出")
        if sha256_file(bundle / "neighbors.jsonl") != manifest.get("neighbors_sha256"):
            problems.append("neighbors.jsonl 与 manifest.json 记录的哈希不一致：文件在传输中损坏或被改过")
        if export_info.get("paragraph_fingerprint") != paragraph_fingerprint(db):
            problems.append("证据段落自导出以来变了（索引被 rebuild 过？）：段落编号对不上，需要重新导出")
        if manifest.get("fake") and not allow_fake:
            problems.append("这是 --fake-encoder 的产物，只用于联调，不能装成正式结果（确要装加 --allow-fake）")
        if manifest.get("limit"):
            problems.append(f"GPU 端用了 --limit {manifest['limit']}，结果不完整")
        if problems:
            return {"installed": False, "problems": problems}
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / "neighbors.jsonl", target / "neighbors.jsonl")
        installed = {**manifest, "paragraph_fingerprint": export_info["paragraph_fingerprint"]}
        (target / "manifest.json").write_text(json.dumps(installed, ensure_ascii=False, indent=2), encoding="utf-8")
        _CACHE.clear()
        return {"installed": True, "model": manifest.get("model"), "queries": manifest.get("queries"),
                "target": str(target)}
    finally:
        if own:
            db.close()


# ---------------------------------------------------------------------------
# lookup and fusion
# ---------------------------------------------------------------------------

_CACHE: dict[str, Any] = {}


def load(db: sqlite3.Connection, target: Path = INSTALL_DIR) -> dict[str, list[int]] | None:
    """The installed neighbours, or None if absent or stale.  Checked once per process."""
    key = str(target)
    if key in _CACHE:
        return _CACHE[key]
    neighbors = None
    manifest_path = target / "manifest.json"
    if manifest_path.exists() and (target / "neighbors.jsonl").exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("paragraph_fingerprint") == paragraph_fingerprint(db):
            neighbors = {}
            with (target / "neighbors.jsonl").open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        row = json.loads(line)
                        neighbors[row["id"]] = [int(hit[0]) for hit in row["hits"]]
    _CACHE[key] = neighbors
    return neighbors


def status(db: sqlite3.Connection | None = None, target: Path = INSTALL_DIR) -> dict[str, Any]:
    own = db is None
    db = db or connect()
    try:
        manifest_path = target / "manifest.json"
        if not manifest_path.exists():
            return {"installed": False, "reason": "未安装向量近邻（BM25 单路运行）"}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        fresh = manifest.get("paragraph_fingerprint") == paragraph_fingerprint(db)
        claims = db.execute("SELECT count(*) FROM event_claims").fetchone()[0]
        return {"installed": True, "fresh": fresh, "model": manifest.get("model"),
                "built_at": manifest.get("built_at"), "claims_covered": manifest.get("queries"),
                "claims_now": claims}
    finally:
        if own:
            db.close()


def rrf(*ranked_lists: list[int], k: int = RRF_K) -> list[int]:
    """Reciprocal-rank fusion: each list votes 1/(k + rank) for its items.  Ties keep first-seen order."""
    scores: dict[int, float] = {}
    order: dict[int, int] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            order.setdefault(item, len(order))
    return sorted(scores, key=lambda item: (-scores[item], order[item]))


def fetch_paragraphs(db: sqlite3.Connection, ids: list[int], company_id: str,
                     scores: dict[int, float] | None = None) -> list[dict[str, Any]]:
    """Rows in the order given, shaped like ``claim_index.search`` results.

    ``score`` stays the BM25 score where BM25 found the paragraph, so the stored
    ``retrieval_score`` keeps one meaning; a paragraph only the vectors found
    has no BM25 score and gets None rather than a number on another scale.
    """
    scores = scores or {}
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = {row[0]: row for row in db.execute(
        f"""SELECT paragraph_id,version_id,title,section,page,text FROM evidence_paragraphs
            WHERE company_id=? AND paragraph_id IN ({placeholders})""", [company_id, *ids])}
    return [{"paragraph_id": rows[pid][0], "version_id": rows[pid][1], "title": rows[pid][2],
             "section": rows[pid][3], "page": rows[pid][4], "text": rows[pid][5], "score": scores.get(pid)}
            for pid in ids if pid in rows]


def hybrid_search(db: sqlite3.Connection, company_id: str, query: str, claim_ids: list[str],
                  limit: int = 5, target: Path = INSTALL_DIR) -> list[dict[str, Any]]:
    """BM25 and dense neighbours fused by RRF; plain BM25 when no neighbours exist for this claim."""
    from processor.claim_index import search
    neighbors = load(db, target)
    dense = next((neighbors[claim_id] for claim_id in claim_ids if neighbors and claim_id in neighbors), None)
    if dense is None:
        return search(db, company_id, query, limit)
    lexical_hits = search(db, company_id, query, FUSION_DEPTH)
    lexical = [hit["paragraph_id"] for hit in lexical_hits]
    return fetch_paragraphs(db, rrf(lexical, dense[:FUSION_DEPTH])[:limit], company_id,
                            {hit["paragraph_id"]: hit["score"] for hit in lexical_hits})


def fallback_search(db: sqlite3.Connection, company_id: str, query: str, claim_ids: list[str],
                    limit: int = 5, extra: int = FALLBACK_EXTRA,
                    target: Path = INSTALL_DIR) -> list[dict[str, Any]]:
    """BM25's top ``limit`` exactly as they are, then up to ``extra`` paragraphs only the vectors found.

    Why not fusion: equal-weight RRF passed neither half of its adoption rule
    on 2026-10-04 (cross-lingual R@5 +0.52pp against a +10pp bar; verbatim
    -2.73pp against a 2pp allowance) because paragraphs both lists liked
    pushed BM25's fourth and fifth hits out.  Appending cannot do that: the
    candidate list is a superset of BM25's, so recall can only stay or rise.
    The appended rows carry ``score=None`` -- they have no BM25 score -- which
    is also how a stored verdict that rests on one can be found afterwards.
    """
    from processor.claim_index import search
    base = search(db, company_id, query, limit)
    neighbors = load(db, target)
    dense = next((neighbors[claim_id] for claim_id in claim_ids if neighbors and claim_id in neighbors), None)
    if not dense or extra <= 0:
        return base
    seen = {hit["paragraph_id"] for hit in base}
    added = fetch_paragraphs(db, [pid for pid in dense if pid not in seen], company_id)[:extra]
    for hit in added:
        hit["via"] = "dense"
    return base + added


def fallback_accepts(result: dict[str, Any]) -> bool:
    """Whether a fallback re-adjudication may replace the stored UNVERIFIABLE.

    Only a SUPPORTED verdict that rests on a paragraph the vectors added.  On
    2026-10-04 a 20-claim stratified check of the first fallback run found all
    6 such SUPPORTED verdicts right -- most of them English claims matched to
    the traditional-Chinese text of the same HKEX announcement -- and 0 of 8
    CONFLICT verdicts right: a dense neighbour is *about the same kind of
    thing* (another buy-back, another dividend, another period's revenue), and
    reading a different event as a contradiction is exactly the mistake that
    produces.  Agreement with a similar paragraph is evidence; disagreement
    with one is not.  Anything else -- including a different answer on the
    same BM25 evidence, which is the model's own run-to-run variance -- keeps
    the verdict already stored.
    """
    candidate = result.get("candidate") or {}
    return result.get("verdict") == "SUPPORTED" and candidate.get("via") == "dense"


def fallback_targets(db: sqlite3.Connection, target: Path = INSTALL_DIR) -> list[str]:
    """UNVERIFIABLE claims the fallback would show at least one new paragraph."""
    neighbors = load(db, target)
    if neighbors is None:
        return []
    rows = db.execute("""SELECT c.claim_id,c.text,e.company_id FROM event_claims c
        JOIN event_records e ON e.event_id=c.event_id
        JOIN claim_verdicts v ON v.claim_id=c.claim_id
        WHERE v.verdict='UNVERIFIABLE' AND e.company_id IS NOT NULL""").fetchall()
    return sorted(claim_id for claim_id, text, company_id in rows
                  if len(fallback_search(db, company_id, text, [claim_id], target=target))
                  > len(fallback_search(db, company_id, text, [claim_id], extra=0, target=target)))


def changed_claims(db: sqlite3.Connection, limit: int = 5, target: Path = INSTALL_DIR) -> list[str]:
    """Claims whose top-``limit`` evidence differs between BM25 and hybrid recall.

    Only these can get a different verdict from switching retrieval, so they
    are the ones worth re-adjudicating (``claim_verdict --retrieval hybrid
    --claims-file``); re-running every claim would pay for thousands of
    judgements on identical evidence.
    """
    from processor.claim_index import search
    if load(db, target) is None:
        return []
    rows = db.execute("""SELECT c.claim_id,c.text,e.company_id FROM event_claims c
        JOIN event_records e ON e.event_id=c.event_id WHERE e.company_id IS NOT NULL""").fetchall()
    changed = []
    for claim_id, text, company_id in rows:
        lexical = {hit["paragraph_id"] for hit in search(db, company_id, text, limit)}
        fused = {hit["paragraph_id"] for hit in hybrid_search(db, company_id, text, [claim_id], limit, target)}
        if lexical != fused:
            changed.append(claim_id)
    return sorted(changed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dense recall: export for the GPU server, import its result")
    sub = parser.add_subparsers(dest="command", required=True)
    out = sub.add_parser("export", help="write the bundle the GPU server needs")
    out.add_argument("--out", required=True, type=Path)
    back = sub.add_parser("import", help="validate and install neighbours from the GPU server")
    back.add_argument("--bundle", required=True, type=Path)
    back.add_argument("--allow-fake", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("status", help="what is installed and whether it is still valid")
    diff = sub.add_parser("changed", help="list claims whose top-5 evidence changes under hybrid recall")
    diff.add_argument("--out", required=True, type=Path)
    fall = sub.add_parser("fallback-targets",
                          help="list UNVERIFIABLE claims the fallback mode would show new evidence")
    fall.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command in ("changed", "fallback-targets"):
        db = connect()
        db.row_factory = sqlite3.Row
        claims = changed_claims(db) if args.command == "changed" else fallback_targets(db)
        db.close()
        args.out.write_text("".join(f"{claim_id}\n" for claim_id in claims), encoding="utf-8")
        print(json.dumps({"changed": len(claims), "out": str(args.out)}, ensure_ascii=False))
        return 0
    if args.command == "export":
        result = export(args.out)
    elif args.command == "import":
        result = install(args.bundle, allow_fake=args.allow_fake)
    else:
        result = status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("installed", True) else 1


if __name__ == "__main__":
    sys.exit(main())
