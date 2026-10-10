#!/usr/bin/env python3
"""Review a manually imported immutable evidence version."""
from __future__ import annotations

import argparse

from collector.collect import audit, connect, now_iso
from collector.manual_import import ensure_provenance_table
from collector.report import main as write_report

DECISIONS = ("verified-mirror", "verified-official", "reject")
STATUS_BY_DECISION = {
    "verified-mirror": "manual_import_verified_mirror",
    "verified-official": "manual_import_verified_official",
    "reject": "manual_import_rejected",
}


def review_version(version_id: str, decision: str, reviewer: str, note: str,
                   *, write_ledger: bool = True) -> dict[str, str]:
    if decision not in DECISIONS:
        raise ValueError(f"invalid review decision: {decision}")
    if not reviewer.strip() or not note.strip():
        raise ValueError("reviewer and note are required")
    db = connect()
    try:
        ensure_provenance_table(db)
        row = db.execute("""SELECT v.source_id,v.canonical_url,v.content_hash,
            p.equivalence_status,p.acquisition_source_tier
            FROM document_versions v JOIN manual_import_provenance p
              ON p.version_id=v.version_id WHERE v.version_id=?""", (version_id,)).fetchone()
        if not row:
            raise ValueError(f"manual import version not found: {version_id}")
        source_id, canonical_url, content_hash, equivalence_status, source_tier = row
        if decision == "verified-official" and not (
                equivalence_status == "exact_official_hash" and source_tier in {"S0", "S1"}):
            raise ValueError(
                "verified-official requires exact_official_hash provenance at S0/S1")
        status = STATUS_BY_DECISION[decision]
        reviewed_at = now_iso()
        db.execute("""UPDATE manual_import_provenance SET review_status=?,review_note=?,
            reviewed_at=? WHERE version_id=?""",
                   (decision, f"{reviewer}: {note}", reviewed_at, version_id))
        db.execute("UPDATE document_versions SET review_status=? WHERE version_id=?",
                   (status, version_id))
        current = db.execute("""SELECT content_hash FROM documents
            WHERE source_id=? AND canonical_url=?""", (source_id, canonical_url)).fetchone()
        if current and current[0] == content_hash:
            db.execute("""UPDATE documents SET review_status=?
                WHERE source_id=? AND canonical_url=?""", (status, source_id, canonical_url))
            db.execute("""UPDATE discovered_metadata SET review_status=?
                WHERE source_id=? AND canonical_url=?""", (status, source_id, canonical_url))
        db.commit()
    finally:
        db.close()
    audit({"source_id": source_id, "outcome": "manual_import_reviewed",
           "version_id": version_id, "url": canonical_url, "decision": decision,
           "review_status": status, "reviewer": reviewer, "note": note})
    if write_ledger:
        write_report()
    return {"version_id": version_id, "source_id": source_id,
            "canonical_url": canonical_url, "review_status": status}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review one manually imported evidence version")
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--decision", required=True, choices=DECISIONS)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--note", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = review_version(args.version_id, args.decision, args.reviewer, args.note)
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
