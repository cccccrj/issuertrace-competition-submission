#!/usr/bin/env python3
"""Low-volume collection through the documented Federal Register public API."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from collector.collect import (
    ROOT,
    api_job_outcome,
    audit,
    canonicalize,
    collect_source,
    connect,
    enqueue_frontier,
    finish_frontier,
    load_sources,
)

SOURCE_ID = "us_federal_register_api"
ENDPOINT = "https://www.federalregister.gov/api/v1/documents.json"
TERMS = {
    "cxmt": ("ChangXin Memory Technologies", "CXMT"),
    "wuxi_apptec": ("WuXi AppTec",),
}


def ensure_jobs(db, config: dict, today: date | None = None) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS federal_register_jobs (
        job_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, search_term TEXT NOT NULL,
        page_no INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, processed_at TEXT,
        start_date TEXT, end_date TEXT)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(federal_register_jobs)")}
    for name in ("start_date", "end_date"):
        if name not in columns:
            db.execute(f"ALTER TABLE federal_register_jobs ADD COLUMN {name} TEXT")
    backfill_start = config["scope"]["backfill_start"]
    snapshot = config["scope"]["snapshot_as_of"]
    for company_id, terms in TERMS.items():
        for term in terms:
            job_id = f"{company_id}:{term}:1"
            db.execute("""INSERT OR IGNORE INTO federal_register_jobs
                (job_id,company_id,search_term,page_no,start_date,end_date)
                VALUES(?,?,?,1,?,?)""", (job_id, company_id, term, backfill_start, snapshot))
    db.execute("""UPDATE federal_register_jobs SET start_date=COALESCE(start_date,?),
        end_date=COALESCE(end_date,?)""", (backfill_start, snapshot))
    incremental_day = date.fromisoformat(snapshot) + timedelta(days=1)
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    while incremental_day <= today:
        for company_id, terms in TERMS.items():
            for term in terms:
                job_id = f"incremental:{company_id}:{term}:{incremental_day.isoformat()}:1"
                db.execute("""INSERT OR IGNORE INTO federal_register_jobs
                    (job_id,company_id,search_term,page_no,start_date,end_date)
                    VALUES(?,?,?,1,?,?)""",
                           (job_id, company_id, term, incremental_day.isoformat(),
                            incremental_day.isoformat()))
        incremental_day += timedelta(days=1)
    db.commit()


def result_metadata(item: dict) -> tuple[str | None, str | None, str]:
    """Return title, publication date, and normalized document type."""
    kind = str(item.get("type") or "document").strip().lower().replace(" ", "_")
    return item.get("title"), item.get("publication_date"), kind


def store_hint(db, config: dict, company_id: str, url: str, title: str | None,
               published_at: str | None, document_type: str) -> None:
    legal_name = config["entities"][company_id]["canonical_name"]
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,published_at=excluded.published_at,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,document_type=excluded.document_type,
        original_timezone=excluded.original_timezone,review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, published_at, company_id, legal_name,
         document_type, "America/New_York", "candidate_entity_match"))


def fetch_one_queued_pdf(db, source, user_agent: str) -> int:
    row = db.execute("""SELECT url FROM crawl_frontier
        WHERE source_id=? AND status='pending' AND lower(url) LIKE '%.pdf%'
        ORDER BY discovered_at,url LIMIT 1""", (SOURCE_ID,)).fetchone()
    if not row:
        return 0
    url = row[0]
    collect_source(db, source, user_agent, 1, False, url)
    saved = db.execute("SELECT 1 FROM documents WHERE source_id=? AND canonical_url=?",
                       (SOURCE_ID, canonicalize(url))).fetchone()
    if saved:
        finish_frontier(db, SOURCE_ID, url, "done")
        return 1
    return 0


def process_one(contact: str) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_jobs(db, config)
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    job = db.execute("""SELECT job_id,company_id,search_term,page_no,attempts,start_date,end_date
        FROM federal_register_jobs WHERE status='pending'
        ORDER BY attempts,page_no,company_id,search_term LIMIT 1""").fetchone()
    if not job:
        fetched = fetch_one_queued_pdf(db, source, user_agent)
        db.close()
        return fetched

    job_id, company_id, term, page_no, attempts, start_date, end_date = job
    params = {
        "per_page": 100,
        "page": page_no,
        "conditions[term]": term,
        "conditions[publication_date][gte]": start_date,
        "conditions[publication_date][lte]": end_date,
    }
    url = ENDPOINT + "?" + urlencode(params)
    store_hint(db, config, company_id, url, f"Federal Register API search: {term}", None, "api_response")
    db.commit()
    collect_source(db, source, user_agent, 1, False, url)
    row = db.execute("SELECT raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                     (SOURCE_ID, canonicalize(url))).fetchone()
    try:
        if not row:
            raise ValueError("API response was not saved; inspect access-control audit events")
        payload = json.loads((ROOT / row[0]).read_text(encoding="utf-8"))
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("Federal Register API response has no results list")
        discovered = 0
        for item in results:
            title, published_at, document_type = result_metadata(item)
            for candidate in (item.get("html_url"), item.get("pdf_url")):
                if not candidate:
                    continue
                store_hint(db, config, company_id, candidate, title, published_at, document_type)
                if urlparse(candidate).hostname == "www.govinfo.gov" and candidate.lower().endswith(".pdf"):
                    enqueue_frontier(db, SOURCE_ID, candidate, canonicalize(url), 0)
                discovered += 1
        total_pages = max(int(payload.get("total_pages") or 1), 1)
        for extra_page in range(page_no + 1, total_pages + 1):
            extra_id = job_id.rsplit(":", 1)[0] + f":{extra_page}"
            db.execute("""INSERT OR IGNORE INTO federal_register_jobs
                (job_id,company_id,search_term,page_no,start_date,end_date)
                VALUES(?,?,?,?,?,?)""",
                       (extra_id, company_id, term, extra_page, start_date, end_date))
        db.execute("""UPDATE federal_register_jobs SET status='done',attempts=?,last_error=NULL,
            processed_at=? WHERE job_id=?""",
                   (attempts + 1, datetime.now(timezone.utc).isoformat(timespec="seconds"), job_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": api_job_outcome(job_id, True), "job_id": job_id,
               "search_term": term, "items_discovered": len(results),
               "urls_discovered": discovered, "page_count": total_pages})
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        status = "manual_review" if attempts + 1 >= 3 else "pending"
        db.execute("""UPDATE federal_register_jobs SET status=?,attempts=?,last_error=?
            WHERE job_id=?""", (status, attempts + 1, str(exc)[:300], job_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": api_job_outcome(job_id, False), "job_id": job_id,
               "url": url, "error": str(exc)[:300]})
    finally:
        db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    process_one(contact)
