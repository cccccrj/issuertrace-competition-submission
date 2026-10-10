#!/usr/bin/env python3
"""On the GPU server: embed evidence paragraphs and claims, write each claim's nearest paragraphs.

This file is deliberately standalone -- it imports nothing from the repository,
so the GPU server needs this one script, the exported bundle and the model, and
never the corpus database or the rest of the code.  The steps around it live in
``processor.dense_index`` on the platform side:

    platform:  python -m processor.dense_index export --out bundle/
    GPU:       python embed_neighbors.py --bundle bundle/ --model /models/bge-m3
    platform:  python -m processor.dense_index import --bundle bundle/

Input (written by ``export``):
    bundle/paragraphs.jsonl   {"id": int, "company": str, "text": str}
    bundle/queries.jsonl      {"id": str,  "company": str, "text": str}
    bundle/export.json        counts and SHA-256 of both files

Output (read by ``import``):
    bundle/neighbors.jsonl    {"id": claim_id, "hits": [[paragraph_id, score], ...]}
    bundle/manifest.json      model, dimensions, timings, and the input hashes it was built from

Only the nearest ``--top-k`` paragraphs *of the claim's own company* are kept: a
claim is never checked against another issuer's filings, and that is the same
scope ``processor.claim_index.search`` searches in.

``--fake-encoder`` swaps the model for a hashed bag of words in pure Python.  It
exists so the whole export -> embed -> import -> evaluate chain can be exercised
on a machine with no GPU and no torch; its neighbours are lexical and its
numbers mean nothing.  Pair it with ``--limit``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections import defaultdict
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
                if limit and len(rows) >= limit:
                    break
    return rows


# ---------------------------------------------------------------------------
# Encoders.  Both return L2-normalised vectors, so a dot product is a cosine.
# ---------------------------------------------------------------------------

class FakeEncoder:
    """Hashed bag of CJK bigrams and Latin words -- for plumbing tests only."""

    name = "fake-hashed-bow"
    DIM = 512
    TOKEN = re.compile(r"[一-鿿]+|[A-Za-z0-9]+")

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * self.DIM
            for match in self.TOKEN.finditer(text or ""):
                token = match.group().lower()
                pieces = ([token[i:i + 2] for i in range(len(token) - 1)] or [token]) \
                    if "一" <= token[0] <= "鿿" else [token]
                for piece in pieces:
                    vector[int(hashlib.md5(piece.encode()).hexdigest(), 16) % self.DIM] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            out.append([value / norm for value in vector])
        return out


class ModelEncoder:
    """A sentence-transformers model (BGE-M3 by default) on the GPU."""

    def __init__(self, model: str, batch_size: int, max_length: int, device: str | None):
        import torch  # noqa: F401  -- fail here, with a clear message, if torch is missing
        from sentence_transformers import SentenceTransformer
        self.name = model
        self.batch_size = batch_size
        self.model = SentenceTransformer(model, device=device)
        self.model.max_seq_length = max_length
        if self.model.device.type == "cuda":
            self.model.half()

    def encode(self, texts: list[str]):
        return self.model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                 convert_to_tensor=True, show_progress_bar=True)


# ---------------------------------------------------------------------------
# Nearest neighbours, per company.
# ---------------------------------------------------------------------------

def topk_python(query_vectors, paragraph_vectors, paragraph_ids, k):
    results = []
    for query in query_vectors:
        scored = [(sum(a * b for a, b in zip(query, vector)), pid)
                  for vector, pid in zip(paragraph_vectors, paragraph_ids)]
        scored.sort(reverse=True)
        results.append([[pid, round(score, 4)] for score, pid in scored[:k]])
    return results


def topk_torch(query_vectors, paragraph_vectors, paragraph_ids, k, chunk=1024):
    import torch
    results = []
    k = min(k, len(paragraph_ids))
    for start in range(0, query_vectors.shape[0], chunk):
        scores = query_vectors[start:start + chunk] @ paragraph_vectors.T
        values, indices = torch.topk(scores.float(), k, dim=1)
        for row_values, row_indices in zip(values.tolist(), indices.tolist()):
            results.append([[paragraph_ids[index], round(value, 4)]
                            for value, index in zip(row_values, row_indices)])
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", required=True, type=Path, help="directory written by dense_index export")
    parser.add_argument("--model", default="BAAI/bge-m3",
                        help="model id or local directory (default: BAAI/bge-m3)")
    parser.add_argument("--top-k", type=int, default=20, help="neighbours kept per claim (default: 20)")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=512, help="tokens per text (default: 512)")
    parser.add_argument("--device", default=None, help="e.g. cuda, cuda:1, cpu (default: auto)")
    parser.add_argument("--fake-encoder", action="store_true", help="plumbing test without a model")
    parser.add_argument("--limit", type=int, default=None, help="only the first N paragraphs and queries")
    args = parser.parse_args(argv)

    bundle: Path = args.bundle
    export = json.loads((bundle / "export.json").read_text(encoding="utf-8"))
    for name in ("paragraphs.jsonl", "queries.jsonl"):
        if sha256_file(bundle / name) != export["sha256"][name]:
            print(f"{name} does not match export.json -- the bundle was altered or copied incompletely",
                  file=sys.stderr)
            return 2

    paragraphs = read_jsonl(bundle / "paragraphs.jsonl", args.limit)
    queries = read_jsonl(bundle / "queries.jsonl", args.limit)
    encoder = FakeEncoder() if args.fake_encoder else ModelEncoder(
        args.model, args.batch_size, args.max_length, args.device)
    topk = topk_python if args.fake_encoder else topk_torch

    by_company_p: dict[str, list[dict]] = defaultdict(list)
    by_company_q: dict[str, list[dict]] = defaultdict(list)
    for row in paragraphs:
        by_company_p[row["company"]].append(row)
    for row in queries:
        by_company_q[row["company"]].append(row)

    started = time.time()
    written = 0
    dimensions = None
    with (bundle / "neighbors.jsonl").open("w", encoding="utf-8") as out:
        for company in sorted(by_company_q):
            pool = by_company_p.get(company, [])
            if not pool:
                continue
            print(f"[{company}] embedding {len(pool)} paragraphs, {len(by_company_q[company])} claims",
                  file=sys.stderr)
            paragraph_vectors = encoder.encode([row["text"] for row in pool])
            query_vectors = encoder.encode([row["text"] for row in by_company_q[company]])
            dimensions = len(paragraph_vectors[0])
            hits = topk(query_vectors, paragraph_vectors, [row["id"] for row in pool], args.top_k)
            for query, row_hits in zip(by_company_q[company], hits):
                out.write(json.dumps({"id": query["id"], "hits": row_hits}, ensure_ascii=False) + "\n")
                written += 1

    manifest = {
        "model": encoder.name,
        "fake": bool(args.fake_encoder),
        "dimensions": dimensions,
        "top_k": args.top_k,
        "max_length": args.max_length,
        "limit": args.limit,
        "paragraphs": len(paragraphs),
        "queries": written,
        "seconds": round(time.time() - started, 1),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "input_sha256": export["sha256"],
        "neighbors_sha256": sha256_file(bundle / "neighbors.jsonl"),
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
