#!/usr/bin/env python3
"""Low-volume exact-firm collection from the documented openFDA enforcement APIs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from collector.collect import (
    ROOT, audit, blocked_host, canonicalize, collect_source, connect, load_sources,
)

SOURCE_ID = "us_fda_openfda"
ENDPOINTS = {
    "drug": "https://api.fda.gov/drug/enforcement.json",
    "device": "https://api.fda.gov/device/enforcement.json",
}
TERMS = {
    "wuxi_apptec": ("wuxi_apptec", "WuXi AppTec"),
    "wuxi_sta": ("wuxi_apptec", "WuXi STA"),
}


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS openfda_enforcement_state (
        term_id TEXT PRIMARY KEY, endpoint_type TEXT NOT NULL, company_id TEXT NOT NULL,
        search_term TEXT NOT NULL, last_completed_at TEXT, total_results INTEGER,
        last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS openfda_enforcement_jobs (
        job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, term_id TEXT NOT NULL,
        mode TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS openfda_enforcement_items (
        item_id TEXT PRIMARY KEY, endpoint_type TEXT NOT NULL, term_id TEXT NOT NULL,
        company_id TEXT NOT NULL, recall_number TEXT, event_id TEXT,
        recalling_firm TEXT NOT NULL, report_date TEXT, recall_initiation_date TEXT,
        classification TEXT, status TEXT, product_description TEXT,
        reason_for_recall TEXT, query_url TEXT NOT NULL, raw_content_hash TEXT NOT NULL,
        review_status TEXT NOT NULL, first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL)""")
    for endpoint_type in ENDPOINTS:
        for term_name, (company_id, term) in TERMS.items():
            term_id = f"{endpoint_type}:{term_name}"
            db.execute("""INSERT INTO openfda_enforcement_state
                (term_id,endpoint_type,company_id,search_term) VALUES(?,?,?,?)
                ON CONFLICT(term_id) DO UPDATE SET endpoint_type=excluded.endpoint_type,
                company_id=excluded.company_id,search_term=excluded.search_term""",
                       (term_id, endpoint_type, company_id, term))
    db.commit()


def ensure_due_runs(db, backfill_start: str, now: datetime) -> None:
    cutoff = now - timedelta(days=7)
    end_date = now.date().isoformat()
    for term_id, last_completed in db.execute("""SELECT term_id,last_completed_at
            FROM openfda_enforcement_state ORDER BY term_id""").fetchall():
        if db.execute("""SELECT 1 FROM openfda_enforcement_jobs
                WHERE term_id=? AND status!='done' LIMIT 1""", (term_id,)).fetchone():
            continue
        if last_completed and datetime.fromisoformat(last_completed) > cutoff:
            continue
        mode = "backfill" if not last_completed else "incremental"
        run_id = f"{mode}:{term_id}:{end_date}"
        db.execute("""INSERT OR IGNORE INTO openfda_enforcement_jobs
            (job_id,run_id,term_id,mode,start_date,end_date)
            VALUES(?,?,?,?,?,?)""",
                   (run_id, run_id, term_id, mode, backfill_start, end_date))
    db.commit()


def suspend_pending_jobs(db, reason: str) -> int:
    changed = db.execute("""UPDATE openfda_enforcement_jobs
        SET status='manual_review',last_error=? WHERE status='pending'""",
                         (reason,)).rowcount
    db.execute("""UPDATE openfda_enforcement_state SET last_error=? WHERE term_id IN
        (SELECT term_id FROM openfda_enforcement_jobs WHERE status='manual_review')""",
               (reason,))
    db.commit()
    return changed


def search_url(endpoint_type: str, term: str, start_date: str, end_date: str) -> str:
    field_range = f"[{start_date.replace('-', '')} TO {end_date.replace('-', '')}]"
    query = f'recalling_firm:"{term}" AND report_date:{field_range}'
    return ENDPOINTS[endpoint_type] + "?" + urlencode({"search": query, "limit": 1000})


def parse_payload(status: int, payload: object) -> tuple[list[dict], int]:
    if status == 404 and isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("code") == "NOT_FOUND":
            return [], 0
    if status != 200 or not isinstance(payload, dict):
        raise ValueError(f"unexpected openFDA response status {status}")
    results = payload.get("results")
    meta_results = (payload.get("meta") or {}).get("results")
    if not isinstance(results, list) or not isinstance(meta_results, dict):
        raise ValueError("openFDA response has no results metadata")
    total = int(meta_results.get("total") or 0)
    if total > 1000:
        raise ValueError(f"exact firm query exceeded single-call safety cap: {total}")
    return [item for item in results if isinstance(item, dict)], total


def exact_items(items: list[dict], term: str) -> list[dict]:
    needle = term.casefold()
    return [item for item in items
            if needle in str(item.get("recalling_firm") or "").casefold()]


def mark_search_response(db, url: str) -> None:
    normalized = canonicalize(url)
    db.execute("""UPDATE documents SET review_status='search_response'
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, normalized))
    db.execute("""UPDATE document_versions SET review_status='search_response',
        document_type='search_response' WHERE source_id=? AND canonical_url=?""",
               (SOURCE_ID, normalized))


def store_query_hint(db, config: dict, company_id: str, url: str, title: str) -> None:
    legal_name = config["entities"][company_id]["canonical_name"]
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,
        original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, company_id, legal_name,
         "search_response", "America/New_York", "search_response"))


def store_items(db, endpoint_type: str, term_id: str, company_id: str,
                items: list[dict], query_url: str, content_hash: str,
                now: str) -> int:
    stored = 0
    for item in items:
        identity = "\n".join(str(item.get(key) or "") for key in (
            "recall_number", "event_id", "recalling_firm", "product_description",
            "report_date"))
        item_id = hashlib.sha256(f"{endpoint_type}\n{identity}".encode()).hexdigest()
        db.execute("""INSERT INTO openfda_enforcement_items
            (item_id,endpoint_type,term_id,company_id,recall_number,event_id,
             recalling_firm,report_date,recall_initiation_date,classification,status,
             product_description,reason_for_recall,query_url,raw_content_hash,
             review_status,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
            raw_content_hash=excluded.raw_content_hash,query_url=excluded.query_url,
            classification=excluded.classification,status=excluded.status,
            product_description=excluded.product_description,
            reason_for_recall=excluded.reason_for_recall""",
            (item_id, endpoint_type, term_id, company_id, item.get("recall_number"),
             item.get("event_id"), str(item.get("recalling_firm") or ""),
             item.get("report_date"), item.get("recall_initiation_date"),
             item.get("classification"), item.get("status"),
             item.get("product_description"), item.get("reason_for_recall"),
             canonicalize(query_url), content_hash, "candidate_entity_match", now, now))
        stored += 1
    return stored


def process_one(contact: str, now: datetime | None = None) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    reason = blocked_host(db, SOURCE_ID, "api.fda.gov")
    if reason or (state and state[0] == "manual_review"):
        reason = reason or state[1] or "source requires manual review"
        changed = suspend_pending_jobs(db, reason)
        if changed:
            audit({"source_id": SOURCE_ID,
                   "outcome": "openfda_enforcement_suspended_manual_review",
                   "error": reason, "jobs_suspended": changed})
        db.close()
        return 0

    current = now or datetime.now(timezone.utc)
    ensure_due_runs(db, source.raw["backfill_start"], current)
    job = db.execute("""SELECT j.job_id,j.term_id,j.mode,j.start_date,j.end_date,
        j.attempts,s.endpoint_type,s.company_id,s.search_term
        FROM openfda_enforcement_jobs j JOIN openfda_enforcement_state s
        ON s.term_id=j.term_id WHERE j.status='pending'
        ORDER BY CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,j.term_id LIMIT 1""").fetchone()
    if not job:
        db.close()
        return 0

    job_id, term_id, mode, start_date, end_date, attempts, endpoint_type, company_id, term = job
    url = search_url(endpoint_type, term, start_date, end_date)
    store_query_hint(db, config, company_id, url,
                     f"openFDA {endpoint_type} enforcement exact firm search: {term}")
    db.commit()
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    collect_source(db, source, user_agent, 1, False, url)
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    try:
        if source_state and source_state[0] == "manual_review":
            suspend_pending_jobs(db, source_state[1] or "source requires manual review")
            raise ValueError(source_state[1] or "source requires manual review")
        row = db.execute("""SELECT status,raw_path,content_hash FROM documents
            WHERE source_id=? AND canonical_url=?""",
                         (SOURCE_ID, canonicalize(url))).fetchone()
        if not row:
            raise ValueError("openFDA response was not saved")
        status, raw_path, content_hash = row
        payload = json.loads((ROOT / raw_path).read_text(encoding="utf-8"))
        results, total = parse_payload(int(status), payload)
        accepted = exact_items(results, term)
        if len(accepted) != len(results):
            raise ValueError("openFDA returned records outside the exact firm phrase")
        processed_at = current.isoformat(timespec="seconds")
        stored = store_items(db, endpoint_type, term_id, company_id, accepted,
                             url, content_hash, processed_at)
        mark_search_response(db, url)
        db.execute("""UPDATE openfda_enforcement_jobs SET status='done',attempts=?,
            last_error=NULL,processed_at=? WHERE job_id=?""",
                   (attempts + 1, processed_at, job_id))
        db.execute("""UPDATE openfda_enforcement_state SET last_completed_at=?,
            total_results=?,last_error=NULL WHERE term_id=?""",
                   (processed_at, total, term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "openfda_enforcement_success",
               "job_id": job_id, "term_id": term_id, "mode": mode,
               "search_term": term, "endpoint_type": endpoint_type,
               "total_results": total, "items_stored": stored})
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        if not (source_state and source_state[0] == "manual_review"):
            status = "manual_review" if attempts + 1 >= 3 else "pending"
            db.execute("""UPDATE openfda_enforcement_jobs SET status=?,attempts=?,
                last_error=? WHERE job_id=?""",
                       (status, attempts + 1, str(exc)[:300], job_id))
            db.execute("UPDATE openfda_enforcement_state SET last_error=? WHERE term_id=?",
                       (str(exc)[:300], term_id))
            db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "openfda_enforcement_error",
               "job_id": job_id, "term_id": term_id, "error": str(exc)[:300]})
    finally:
        db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    process_one(contact)
