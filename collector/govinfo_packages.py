#!/usr/bin/env python3
"""Collect a verified, versioned set of GovInfo legislative PDF packages."""
from __future__ import annotations

from datetime import datetime, timezone

from collector.collect import (
    ROOT, audit, blocked_host, canonicalize, collect_source, connect, load_sources,
)

SOURCE_ID = "us_govinfo"
PACKAGE_ROOT = "https://www.govinfo.gov/content/pkg"
PACKAGES = {
    "BILLS-118hr7085ih": {
        "url": f"{PACKAGE_ROOT}/BILLS-118hr7085ih/pdf/BILLS-118hr7085ih.pdf",
        "title": "H.R. 7085 (IH) — BIOSECURE Act",
        "published_at": "2024-01-25", "document_type": "bill_introduced_house",
    },
    "BILLS-118hr8333ih": {
        "url": f"{PACKAGE_ROOT}/BILLS-118hr8333ih/pdf/BILLS-118hr8333ih.pdf",
        "title": "H.R. 8333 (IH) — BIOSECURE Act",
        "published_at": "2024-05-10", "document_type": "bill_introduced_house",
    },
    "CPRT-118HPRT55711": {
        "url": f"{PACKAGE_ROOT}/CPRT-118HPRT55711/pdf/CPRT-118HPRT55711.pdf",
        "title": "House Oversight full committee markup including H.R. 8333",
        "published_at": "2024-05-15", "document_type": "committee_markup",
    },
    "BILLS-118hr8333eh": {
        "url": f"{PACKAGE_ROOT}/BILLS-118hr8333eh/pdf/BILLS-118hr8333eh.pdf",
        "title": "H.R. 8333 (EH) — BIOSECURE Act",
        "published_at": "2024-09-09", "document_type": "bill_engrossed_house",
    },
    "CREC-2024-09-09-pt1-PgH5051": {
        "url": f"{PACKAGE_ROOT}/CREC-2024-09-09/pdf/CREC-2024-09-09-pt1-PgH5051.pdf",
        "title": "Congressional Record — House debate on H.R. 8333",
        "published_at": "2024-09-09", "document_type": "floor_debate",
    },
    "BILLS-118hr8333rfs": {
        "url": f"{PACKAGE_ROOT}/BILLS-118hr8333rfs/pdf/BILLS-118hr8333rfs.pdf",
        "title": "H.R. 8333 (RFS) — BIOSECURE Act",
        "published_at": "2024-09-10", "document_type": "bill_referred_senate",
    },
    "BILLS-118s3558rs": {
        "url": f"{PACKAGE_ROOT}/BILLS-118s3558rs/pdf/BILLS-118s3558rs.pdf",
        "title": "S. 3558 (RS) — Prohibiting Foreign Access to American Genetic Information Act",
        "published_at": "2024-09-23", "document_type": "bill_reported_senate",
    },
    "CRPT-118srpt229": {
        "url": f"{PACKAGE_ROOT}/CRPT-118srpt229/pdf/CRPT-118srpt229.pdf",
        "title": "S. Rept. 118-229 accompanying S. 3558",
        "published_at": "2024-09-23", "document_type": "committee_report",
    },
    "BILLS-119s3469is": {
        "url": f"{PACKAGE_ROOT}/BILLS-119s3469is/pdf/BILLS-119s3469is.pdf",
        "title": "S. 3469 (IS) — BIOSECURE Act of 2025",
        "published_at": "2025-12-11", "document_type": "bill_introduced_senate",
    },
}


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS govinfo_package_jobs (
        package_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
        published_at TEXT NOT NULL, document_type TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'backfill', status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT)""")
    for package_id, item in PACKAGES.items():
        db.execute("""INSERT INTO govinfo_package_jobs
            (package_id,url,title,published_at,document_type) VALUES(?,?,?,?,?)
            ON CONFLICT(package_id) DO UPDATE SET url=excluded.url,title=excluded.title,
            published_at=excluded.published_at,document_type=excluded.document_type""",
            (package_id, item["url"], item["title"], item["published_at"],
             item["document_type"]))
    db.commit()


def suspend_pending_jobs(db, reason: str) -> int:
    changed = db.execute("""UPDATE govinfo_package_jobs
        SET status='manual_review',last_error=? WHERE status='pending'""",
                         (reason,)).rowcount
    db.commit()
    return changed


def store_hint(db, config: dict, url: str, title: str, published_at: str,
               document_type: str) -> None:
    legal_name = config["entities"]["wuxi_apptec"]["canonical_name"]
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,published_at=excluded.published_at,
        company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,
        original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, published_at, "wuxi_apptec", legal_name,
         document_type, "America/New_York", "candidate_entity_match"))


def process_one(contact: str) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    reason = blocked_host(db, SOURCE_ID, "www.govinfo.gov")
    if reason or (state and state[0] == "manual_review"):
        reason = reason or state[1] or "source requires manual review"
        changed = suspend_pending_jobs(db, reason)
        if changed:
            audit({"source_id": SOURCE_ID,
                   "outcome": "govinfo_packages_suspended_manual_review",
                   "error": reason, "jobs_suspended": changed})
        db.close()
        return 0

    job = db.execute("""SELECT package_id,url,title,published_at,document_type,attempts
        FROM govinfo_package_jobs WHERE status='pending'
        ORDER BY published_at,package_id LIMIT 1""").fetchone()
    if not job:
        db.close()
        return 0
    package_id, url, title, published_at, document_type, attempts = job
    store_hint(db, config, url, title, published_at, document_type)
    db.commit()
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    collect_source(db, source, user_agent, 1, False, url)
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    saved = db.execute("""SELECT status,content_type,raw_path FROM documents
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url))).fetchone()
    if (saved and int(saved[0]) == 200 and "pdf" in str(saved[1]).lower() and
            source_state and source_state[0] == "closed"):
        processed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("""UPDATE govinfo_package_jobs SET status='done',attempts=?,
            last_error=NULL,processed_at=? WHERE package_id=?""",
                   (attempts + 1, processed_at, package_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "govinfo_package_success",
               "package_id": package_id, "url": url, "document_type": document_type})
    else:
        reason = ((source_state[1] if source_state and source_state[1] else None) or
                  "GovInfo PDF response was not saved")
        status = "manual_review" if (source_state and source_state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("""UPDATE govinfo_package_jobs SET status=?,attempts=?,last_error=?
            WHERE package_id=?""", (status, attempts + 1, reason[:300], package_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "govinfo_package_error",
               "package_id": package_id, "url": url, "error": reason[:300]})
    db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    process_one(contact)
