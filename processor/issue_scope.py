#!/usr/bin/env python3
"""Remove issue codes that cannot apply to a company's industry from stored events.

``pipeline.upsert_event`` applies the same rule to everything labelled from now
on; this pass brings what was stored before it into line, and runs again in
``finalize`` so that declaring or changing a company's ``industry`` in
sources.yaml takes effect without relabelling anything.  I-03 found two memory-
chip events filed under 药物研发 — an audit report and a DRAM shipment forecast.

Deterministic, no model calls, idempotent: a second run changes 0 rows.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any

from collector.collect import connect
from processor.pipeline import out_of_scope_codes, set_review_reason

REASON = "issue_out_of_scope"


def backfill(db: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, Any]:
    events = db.execute("SELECT event_id, company_id, issue_codes_json FROM event_records").fetchall()
    claim_codes: dict[str, list[tuple[str, str | None]]] = {}
    for claim_id, event_id, code in db.execute("SELECT claim_id, event_id, issue_code FROM event_claims"):
        claim_codes.setdefault(event_id, []).append((claim_id, code))
    changed_events, changed_claims = 0, 0
    removed: dict[str, int] = {}
    for event_id, company_id, issue_json in events:
        codes = json.loads(issue_json or "[]")
        claims = claim_codes.get(event_id, [])
        dropped = out_of_scope_codes(company_id, [*codes, *(code for _, code in claims)])
        if not dropped:
            continue
        stale = [claim_id for claim_id, code in claims if code in dropped]
        changed_events += 1
        changed_claims += len(stale)
        for code in dropped:
            removed[code] = removed.get(code, 0) + 1
        if dry_run:
            continue
        db.execute("UPDATE event_records SET issue_codes_json=? WHERE event_id=?",
                   (json.dumps([code for code in codes if code not in dropped], ensure_ascii=False), event_id))
        db.executemany("UPDATE event_claims SET issue_code=NULL WHERE claim_id=?", [(cid,) for cid in stale])
        # Someone has to decide which code these belong to; the rule only knows which they cannot.
        set_review_reason(db, event_id, REASON, True)
    if not dry_run:
        db.commit()
    return {"events": changed_events, "claims": changed_claims, "removed": removed, "dry_run": dry_run}


def main() -> None:
    parser = argparse.ArgumentParser(description="Drop issue codes outside each company's declared industry")
    parser.add_argument("--dry-run", action="store_true", help="count what would change, write nothing")
    args = parser.parse_args()
    db = connect()
    print(json.dumps(backfill(db, dry_run=args.dry_run), ensure_ascii=False, indent=2))
    db.close()


if __name__ == "__main__":
    main()
