#!/usr/bin/env python3
"""Time one document through the pipeline, end to end (W9: P95 ≤ 30s per document).

Measured on documents already in the corpus, stage by stage, so a number that
misses the target says which stage to look at.  Nothing is written: the stages
are driven on copies of stored inputs, because a latency probe that mutates the
corpus is a probe you can only run once.

The model call dominates and is the only stage that costs money — a run of ten
documents is a few thousand tokens.  Parsing is measured from the stored raw
file, which is what the collector hands the processor in a real run.

One call is timed per document.  A long document is split into windows that are
labelled together, up to ``WINDOW_PARALLELISM`` at a time, so the honest
end-to-end figure is the measured call times the number of *rounds* those
windows take — not the number of windows.  Both are reported: what was measured,
and the projection it implies.  Timing every window of a 345,000 character
prospectus would cost more than the answer is worth.

Run it from the repository root: ``PYTHONPATH=. .venv/bin/python
scripts/document_latency.py``.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

from collector.collect import ROOT, connect
from processor.ai_label import (WINDOW_PARALLELISM, call_model, chunk_spans, label_contract,
                                make_prompt, probe)
from processor.claim_index import search
from processor.pipeline import extract_text


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def sample(db: sqlite3.Connection, count: int, company: str | None) -> list[sqlite3.Row]:
    clause, parameters = "", []
    if company:
        clause = " AND m.company_id=?"
        parameters.append(company)
    # Ordered by version_id, which is a hash: an arbitrary but repeatable draw.
    return db.execute(f"""SELECT v.version_id, v.title, v.raw_path, v.document_type,
                                 v.published_at, m.company_id
        FROM document_versions v
        JOIN discovered_metadata m ON m.canonical_url=v.canonical_url
        WHERE v.raw_path IS NOT NULL{clause}
        ORDER BY v.version_id LIMIT ?""", [*parameters, count]).fetchall()


def time_document(db: sqlite3.Connection, row: sqlite3.Row, client, model) -> dict[str, Any]:
    stages: dict[str, float] = {}
    started = time.perf_counter()
    text, pages, status, _kind = extract_text(ROOT / row["raw_path"])
    stages["parse_ms"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    prompt = make_prompt(row["company_id"] or "", row["title"] or "", row["published_at"] or "",
                         row["document_type"] or "", text)
    call_model(client, model, prompt, label_contract())
    stages["label_ms"] = (time.perf_counter() - started) * 1000

    # Retrieval is what a claim costs before the verdict model sees it; the
    # verdict call itself is the same shape as the labelling call above.
    started = time.perf_counter()
    search(db, row["company_id"] or "cxmt", text[:400], 5)
    stages["retrieve_ms"] = (time.perf_counter() - started) * 1000

    windows = max(1, len(chunk_spans(text, row["document_type"])))
    return {"version_id": row["version_id"][:12], "chars": len(text), "pages": pages,
            "status": status, "windows": windows,
            **{key: round(value, 1) for key, value in stages.items()},
            "measured_ms": round(sum(stages.values()), 1),
            # What the same document costs when every window is labelled, which
            # is what a real run does — windows go out together, so it is the
            # number of rounds that multiplies, not the number of windows.
            "rounds": -(-windows // WINDOW_PARALLELISM),
            "projected_ms": round(stages["parse_ms"] + stages["retrieve_ms"]
                                  + stages["label_ms"] * -(-windows // WINDOW_PARALLELISM), 1)}


def run(count: int, company: str | None, budget_ms: float) -> dict[str, Any]:
    resolved, why = probe()
    if not resolved:
        return {"error": f"model gateway unavailable: {why}"}
    client, model, _name = resolved
    db = connect()
    db.row_factory = sqlite3.Row
    documents = [time_document(db, row, client, model) for row in sample(db, count, company)]
    db.close()
    measured = [row["measured_ms"] for row in documents]
    projected = [row["projected_ms"] for row in documents]
    return {"documents": len(documents), "budget_ms": budget_ms,
            "p50_ms": round(statistics.median(measured), 1),
            "p95_ms": round(percentile(measured, 0.95), 1),
            "max_ms": round(max(measured), 1),
            "projected_p95_ms": round(percentile(projected, 0.95), 1),
            "projected_max_ms": round(max(projected), 1),
            "windows_max": max(row["windows"] for row in documents),
            "within_budget": percentile(measured, 0.95) <= budget_ms,
            "projected_within_budget": percentile(projected, 0.95) <= budget_ms,
            "slowest_stage": max(("parse_ms", "label_ms", "retrieve_ms"),
                                 key=lambda stage: statistics.median(
                                     row[stage] for row in documents)),
            "per_document": documents}


def main() -> None:
    parser = argparse.ArgumentParser(description="Time documents through parse, label and retrieval")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--company")
    parser.add_argument("--budget-ms", type=float, default=30_000)
    args = parser.parse_args()
    print(json.dumps(run(args.count, args.company, args.budget_ms), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
