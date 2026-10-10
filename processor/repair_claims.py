#!/usr/bin/env python3
"""Repair legacy claim offsets against preserved parsed text."""
from __future__ import annotations

import json
import re

from collector.collect import ROOT, audit, connect
from processor.pipeline import ensure_schema, set_review_reason, source_locator


def find_span(text: str, quote: str) -> tuple[int, int, str] | None:
    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote), quote
    parts = [re.escape(part) for part in re.split(r"\s+", quote.strip()) if part]
    match = re.search(r"\s+".join(parts), text) if parts else None
    if not match:
        return None
    start, end = match.span()
    return start, end, text[start:end]


def repair() -> dict[str, int]:
    db = connect()
    db.row_factory = __import__("sqlite3").Row
    ensure_schema(db)
    orphaned = db.execute("""SELECT count(*) FROM event_claims c
        LEFT JOIN event_records e ON e.event_id=c.event_id WHERE e.event_id IS NULL""").fetchone()[0]
    db.execute("""DELETE FROM event_claims WHERE event_id NOT IN
        (SELECT event_id FROM event_records)""")
    rows = db.execute("""SELECT c.claim_id,c.event_id,c.text,c.source_start,c.source_end,p.text_path
        FROM event_claims c JOIN event_records e ON e.event_id=c.event_id
        JOIN processed_documents p ON p.version_id=e.version_id""").fetchall()
    repaired = dropped = unchanged = 0
    cache: dict[str, str] = {}
    for row in rows:
        text = cache.setdefault(row["text_path"], (ROOT / row["text_path"]).read_text(encoding="utf-8"))
        if text[row["source_start"]:row["source_end"]] == row["text"]:
            unchanged += 1
            continue
        span = find_span(text, row["text"])
        if span:
            start, end, quote = span
            db.execute("""UPDATE event_claims SET text=?,source_start=?,source_end=?,cited_source=?
                WHERE claim_id=?""", (quote, start, end, source_locator(text, start, end), row["claim_id"]))
            repaired += 1
        else:
            db.execute("DELETE FROM event_claims WHERE claim_id=?", (row["claim_id"],))
            set_review_reason(db, row["event_id"], "unsupported_claim", True)
            dropped += 1
    # An event the model returned no claims for cannot enter the M2-5 → M3
    # verification chain at all.  Tagging it is what lets the platform exclude it
    # from 核验 views instead of showing an event with nothing to verify.
    claimless = [row[0] for row in db.execute("""SELECT e.event_id FROM event_records e
        LEFT JOIN event_claims c ON c.event_id=e.event_id
        GROUP BY e.event_id HAVING count(c.claim_id)=0""")]
    for event_id in claimless:
        set_review_reason(db, event_id, "no_claims", True)
    db.commit()
    db.close()
    summary = {"claims_seen": len(rows), "unchanged": unchanged, "repaired": repaired,
               "unsupported_dropped": dropped, "orphaned_dropped": orphaned,
               "events_without_claims": len(claimless)}
    audit({"source_id": "system", "outcome": "processed_claim_offsets_repaired", **summary})
    return summary


def main() -> None:
    summary = repair()
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
