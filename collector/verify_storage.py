#!/usr/bin/env python3
"""Verify preserved raw files against immutable version hashes without network access."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from collector.collect import ROOT, audit, connect, now_iso


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    db = connect()
    rows = db.execute("""SELECT raw_path,content_hash FROM document_versions
        GROUP BY raw_path,content_hash ORDER BY raw_path""").fetchall()
    checked = 0
    missing: list[str] = []
    mismatched: list[str] = []
    for raw_path, expected_hash in rows:
        path = ROOT / raw_path if raw_path else None
        if not path or not path.is_file():
            missing.append(raw_path or "<empty>")
            continue
        checked += 1
        if sha256_file(path) != expected_hash:
            mismatched.append(raw_path)
    outcome = "storage_verification_success" if not missing and not mismatched else "storage_verification_error"
    event = {
        "source_id": "system", "outcome": outcome, "checked_files": checked,
        "missing_files": len(missing), "hash_mismatches": len(mismatched),
        "missing_samples": missing[:10], "mismatch_samples": mismatched[:10],
    }
    db.execute("""INSERT INTO maintenance_state(job_id,last_run_at,status,details)
        VALUES('verify_storage',?,?,?) ON CONFLICT(job_id) DO UPDATE SET
        last_run_at=excluded.last_run_at,status=excluded.status,details=excluded.details""",
        (now_iso(), outcome, json.dumps(event, ensure_ascii=False)))
    db.commit()
    db.close()
    audit(event)
    print(f"checked_files={checked} missing_files={len(missing)} hash_mismatches={len(mismatched)}")
    return 0 if outcome.endswith("success") else 1


if __name__ == "__main__":
    sys.exit(main())
