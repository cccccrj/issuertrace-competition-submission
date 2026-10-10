#!/usr/bin/env python3
"""Low-volume adapter for BIS's same-origin public site-search endpoint."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin, urlparse

from collector.collect import (
    ROOT, audit, blocked_host, canonicalize, collect_source, connect, enqueue_frontier,
    finish_frontier, load_sources,
)

SOURCE_ID = "us_bis"
ENDPOINT = "https://www.bis.gov/api/es-search"
TERMS = {
    "cxmt_legal": ("cxmt", "ChangXin Memory Technologies"),
    "cxmt_short": ("cxmt", "CXMT"),
    "wuxi_legal": ("wuxi_apptec", "WuXi AppTec"),
}


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS bis_search_state (
        term_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, search_term TEXT NOT NULL,
        last_completed_at TEXT, total_results INTEGER, last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS bis_search_jobs (
        job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, term_id TEXT NOT NULL,
        mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, processed_at TEXT)""")
    for term_id, (company_id, term) in TERMS.items():
        db.execute("""INSERT INTO bis_search_state(term_id,company_id,search_term)
            VALUES(?,?,?) ON CONFLICT(term_id) DO UPDATE SET
            company_id=excluded.company_id,search_term=excluded.search_term""",
                   (term_id, company_id, term))
    db.commit()


def ensure_due_runs(db, now: datetime) -> None:
    cutoff = now - timedelta(days=7)
    for term_id, last_completed in db.execute("""SELECT term_id,last_completed_at
            FROM bis_search_state ORDER BY term_id""").fetchall():
        if db.execute("""SELECT 1 FROM bis_search_jobs WHERE term_id=?
                AND status!='done' LIMIT 1""", (term_id,)).fetchone():
            continue
        if last_completed and datetime.fromisoformat(last_completed) > cutoff:
            continue
        mode = "backfill" if not last_completed else "incremental"
        run_id = f"{mode}:{term_id}:{now.date().isoformat()}"
        db.execute("""INSERT OR IGNORE INTO bis_search_jobs
            (job_id,run_id,term_id,mode) VALUES(?,?,?,?)""",
                   (run_id, run_id, term_id, mode))
    db.commit()


def suspend_pending_jobs(db, reason: str) -> int:
    changed = db.execute("""UPDATE bis_search_jobs SET status='manual_review',last_error=?
        WHERE status='pending'""", (reason,)).rowcount
    db.execute("""UPDATE bis_search_state SET last_error=? WHERE term_id IN
        (SELECT term_id FROM bis_search_jobs WHERE status='manual_review')""", (reason,))
    db.commit()
    return changed


def search_url(term: str) -> str:
    return ENDPOINT + "?" + urlencode({"query": term, "searchCategory": "all"})


def plain(value: object) -> str:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()


def result_items(payload: object, exact_term: str) -> list[tuple[str, str, str]]:
    """Return exact-entity BIS result URL, title and normalized document type."""
    if not isinstance(payload, list):
        raise ValueError("BIS search response is not a result array")
    results: list[tuple[str, str, str]] = []
    for hit in payload:
        if not isinstance(hit, dict) or not isinstance(hit.get("_source"), dict):
            continue
        source = hit["_source"]
        title = plain(source.get("title"))
        body = plain(source.get("body"))
        if exact_term.casefold() not in (title + "\n" + body).casefold():
            continue
        candidate = canonicalize(urljoin("https://www.bis.gov/", plain(source.get("url"))))
        parsed = urlparse(candidate)
        if parsed.hostname != "www.bis.gov" or parsed.scheme != "https":
            continue
        lowered = (title + " " + body).lower()
        if "entity list" in lowered or "restricted party" in lowered:
            document_type = "entity_list"
        elif "export control" in lowered or "export administration" in lowered:
            document_type = "export_controls"
        elif "rule" in lowered or "regulation" in lowered:
            document_type = "rule_or_regulation"
        elif "enforcement" in lowered or "penalty" in lowered:
            document_type = "enforcement"
        else:
            document_type = "bis_official_content"
        item = (candidate, title or candidate.rsplit("/", 1)[-1], document_type)
        if item not in results:
            results.append(item)
    return results


def store_hint(db, config: dict, company_id: str, url: str, title: str,
               document_type: str, matched_entity: str,
               review_status: str = "candidate_entity_match") -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, company_id, matched_entity,
         document_type, "America/New_York", review_status))


def mark_search_response(db, url: str) -> None:
    """Ensure a query URL never counts as a target document, including on HTTP 304."""
    normalized = canonicalize(url)
    db.execute("""UPDATE documents SET review_status='search_response'
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, normalized))
    db.execute("""UPDATE document_versions SET review_status='search_response',
        document_type='search_response' WHERE source_id=? AND canonical_url=?""",
               (SOURCE_ID, normalized))


def fetch_one_candidate(db, source, user_agent: str) -> int:
    row = db.execute("""SELECT url FROM crawl_frontier WHERE source_id=? AND status='pending'
        AND discovered_from LIKE 'bis_search:%'
        ORDER BY discovered_at,url LIMIT 1""", (SOURCE_ID,)).fetchone()
    if not row:
        return 0
    url = row[0]
    collect_source(db, source, user_agent, 1, False, url)
    saved = db.execute("SELECT 1 FROM documents WHERE source_id=? AND canonical_url=?",
                       (SOURCE_ID, canonicalize(url))).fetchone()
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    if saved and state and state[0] == "closed":
        finish_frontier(db, SOURCE_ID, url, "done")
    elif state and state[0] == "manual_review":
        finish_frontier(db, SOURCE_ID, url, "manual_review", state[1])
    return 1


def process_one(contact: str, now: datetime | None = None) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    reason = blocked_host(db, SOURCE_ID, "www.bis.gov")
    if reason or (state and state[0] == "manual_review"):
        reason = reason or state[1] or "source requires manual review"
        changed = suspend_pending_jobs(db, reason)
        if changed:
            audit({"source_id": SOURCE_ID, "outcome": "bis_search_suspended_manual_review",
                   "error": reason, "jobs_suspended": changed})
        db.close()
        return 0

    current = now or datetime.now(timezone.utc)
    ensure_due_runs(db, current)
    job = db.execute("""SELECT j.job_id,j.term_id,j.mode,j.attempts,
        s.company_id,s.search_term FROM bis_search_jobs j
        JOIN bis_search_state s ON s.term_id=j.term_id
        WHERE j.status='pending' ORDER BY CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,
        j.term_id LIMIT 1""").fetchone()
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    if not job:
        fetched = fetch_one_candidate(db, source, user_agent)
        db.close()
        return fetched

    job_id, term_id, mode, attempts, company_id, term = job
    url = search_url(term)
    store_hint(db, config, company_id, url, f"BIS exact search: {term}",
               "search_response", term, "search_response")
    db.commit()
    collect_source(db, source, user_agent, 1, False, url)
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    try:
        if source_state and source_state[0] == "manual_review":
            suspend_pending_jobs(db, source_state[1] or "source requires manual review")
            raise ValueError(source_state[1] or "source requires manual review")
        row = db.execute("SELECT raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                         (SOURCE_ID, canonicalize(url))).fetchone()
        if not row:
            raise ValueError("BIS search response was not saved")
        payload = json.loads((ROOT / row[0]).read_text(encoding="utf-8"))
        results = result_items(payload, term)
        if len(payload) > 500:
            raise ValueError(f"exact search returned implausible result count {len(payload)}")
        mark_search_response(db, url)
        skipped_old = 0
        for candidate, title, document_type in results:
            store_hint(db, config, company_id, candidate, title, document_type, term)
            enqueue_frontier(db, SOURCE_ID, candidate, f"bis_search:{term_id}", 0)
        processed_at = current.isoformat(timespec="seconds")
        db.execute("""UPDATE bis_search_jobs SET status='done',attempts=?,last_error=NULL,
            processed_at=? WHERE job_id=?""", (attempts + 1, processed_at, job_id))
        db.execute("""UPDATE bis_search_state SET last_completed_at=?,total_results=?,
            last_error=NULL WHERE term_id=?""", (processed_at, len(payload), term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "bis_search_success",
               "job_id": job_id, "term_id": term_id, "search_term": term,
               "total_results": len(payload), "results_discovered": len(results),
               "results_skipped_before_boundary": skipped_old})
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        if not (source_state and source_state[0] == "manual_review"):
            status = "manual_review" if attempts + 1 >= 3 else "pending"
            db.execute("""UPDATE bis_search_jobs SET status=?,attempts=?,last_error=?
                WHERE job_id=?""", (status, attempts + 1, str(exc)[:300], job_id))
            db.execute("UPDATE bis_search_state SET last_error=? WHERE term_id=?",
                       (str(exc)[:300], term_id))
            db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "bis_search_error",
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
