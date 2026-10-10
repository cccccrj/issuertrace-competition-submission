#!/usr/bin/env python3
"""Exact WuXi AppTec search for Fierce Biotech and Fierce Pharma."""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin, urlparse

import requests

from collector.collect import (
    ROOT, active_cooldown, audit, block_host, blocked_host, canonicalize,
    challenge_marker, collect_source, connect, load_sources, record_document,
    reserve_host_slot, robot_allowed, save_raw, session_for, set_source_state,
)

SOURCE_IDS = ("fierce_biotech", "fierce_pharma")
TERM = "WuXi AppTec"
PAGE_SIZE = 10
MAX_PAGES = 30
# Public frontend bundle interceptor adds this static key to /jsonapi requests.
REQUEST_KEY = "a7b8c9d0e1f2g3h4i5j6k7l8m9n0o1p2q3r4s5t6u7v8w9x0y1z2"
SITE = {
    "fierce_biotech": ("www.fiercebiotech.com", "fiercebiotech_com"),
    "fierce_pharma": ("www.fiercepharma.com", "fiercepharma_com"),
}


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS fierce_search_state (
        source_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, search_term TEXT NOT NULL,
        last_completed_at TEXT, total_results INTEGER, pages_scanned INTEGER,
        exact_results INTEGER, skipped_before_window INTEGER, last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS fierce_search_jobs (
        job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
        page INTEGER NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        skipped_before_window INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS fierce_detail_jobs (
        source_id TEXT NOT NULL, url TEXT NOT NULL, company_id TEXT NOT NULL,
        title TEXT NOT NULL, published_at TEXT NOT NULL, document_type TEXT NOT NULL,
        mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, processed_at TEXT,
        first_seen_at TEXT NOT NULL, PRIMARY KEY(source_id,url))""")
    for source_id in SOURCE_IDS:
        db.execute("""INSERT INTO fierce_search_state(source_id,company_id,search_term)
            VALUES(?,'wuxi_apptec',?) ON CONFLICT(source_id) DO UPDATE SET
            company_id=excluded.company_id,search_term=excluded.search_term""",
                   (source_id, TERM))
    db.commit()


def ensure_due_runs(db, now: datetime, runnable_sources: set[str]) -> None:
    cutoff = now - timedelta(days=7)
    for source_id, completed in db.execute(
            "SELECT source_id,last_completed_at FROM fierce_search_state ORDER BY source_id"):
        if source_id not in runnable_sources:
            continue
        unfinished = db.execute("""SELECT 1 FROM fierce_search_jobs WHERE source_id=?
            AND status NOT IN ('done','manual_review') LIMIT 1""", (source_id,)).fetchone()
        if unfinished:
            continue
        if completed and datetime.fromisoformat(completed) > cutoff:
            continue
        mode = "backfill" if not completed else "incremental"
        run_id = f"{mode}:{source_id}:{now.date().isoformat()}"
        db.execute("""INSERT OR IGNORE INTO fierce_search_jobs
            (job_id,run_id,source_id,page,mode) VALUES(?,?,?,?,?)""",
                   (f"{run_id}:1", run_id, source_id, 1, mode))
    db.commit()


def plain(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(
        re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def normalize_date(value: object) -> str | None:
    match = re.search(r"([12]\d{3})-(\d{2})-(\d{2})", str(value or ""))
    if not match:
        return None
    try:
        return datetime(*(int(part) for part in match.groups())).date().isoformat()
    except ValueError:
        return None


def canonical_from_attributes(attributes: dict, expected_host: str) -> str | None:
    for tag in attributes.get("metatag") or []:
        if not isinstance(tag, dict) or tag.get("tag") != "link":
            continue
        values = tag.get("attributes")
        if isinstance(values, dict) and values.get("rel") == "canonical":
            candidate = canonicalize(str(values.get("href") or ""))
            if urlparse(candidate).scheme == "https" and urlparse(candidate).hostname == expected_host:
                return candidate
    path = attributes.get("path")
    alias = path.get("alias") if isinstance(path, dict) else None
    if alias:
        candidate = canonicalize(urljoin(f"https://{expected_host}/", str(alias)))
        if urlparse(candidate).hostname == expected_host:
            return candidate
    return None


def result_items(payload: object, exact_term: str, expected_host: str,
                 expected_domain: str) -> tuple[int, list[str], list[tuple[str, str, str, str]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Fierce search response has invalid data")
    meta = payload.get("meta")
    total = meta.get("count") if isinstance(meta, dict) else None
    if not isinstance(total, int) or total < 0 or total > 1000:
        raise ValueError("Fierce search response has invalid result count")
    if len(payload["data"]) > PAGE_SIZE:
        raise ValueError("Fierce search response exceeds configured page size")
    dates: list[str] = []
    results: list[tuple[str, str, str, str]] = []
    for item in payload["data"]:
        if not isinstance(item, dict) or item.get("type") != "node--article":
            continue
        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            continue
        domain = str(attributes.get("domain") or "")
        if expected_domain not in {part for part in domain.split(",") if part}:
            continue
        published_at = normalize_date(
            attributes.get("field_date_published") or attributes.get("created"))
        if published_at:
            dates.append(published_at)
        title = plain(attributes.get("title"))
        body = attributes.get("body")
        body_text = ""
        if isinstance(body, dict):
            body_text = plain(body.get("processed") or body.get("value"))
        metadata_text = []
        for tag in attributes.get("metatag") or []:
            values = tag.get("attributes") if isinstance(tag, dict) else None
            if isinstance(values, dict):
                metadata_text.append(plain(values.get("content")))
        context = "\n".join((title, body_text,
            plain(attributes.get("field_introduction")),
            plain(attributes.get("field_fronts_teaser")), *metadata_text))
        if exact_term.casefold() not in context.casefold() or not published_at:
            continue
        candidate = canonical_from_attributes(attributes, expected_host)
        if not candidate:
            continue
        document_type = "sponsored_industry_news" if attributes.get("is_sponsored") else "industry_news"
        row = (candidate, title or candidate.rsplit("/", 1)[-1], published_at, document_type)
        if row not in results:
            results.append(row)
    return total, dates, results


def search_url(source_id: str, page: int) -> str:
    host, domain_id = SITE[source_id]
    return f"https://{host}/jsonapi/index/tektitedev_on_solr_dev?" + urlencode({
        "page[limit]": PAGE_SIZE,
        "page[offset]": (page - 1) * PAGE_SIZE,
        "filter[fulltext]": TERM,
        "sort[field_date_published][direction]": "desc",
        "sort[field_date_published][path]": "field_date_published",
        "filter[field_domain_access]": domain_id,
    })


def store_hint(db, config: dict, source_id: str, url: str, title: str,
               published_at: str | None, document_type: str,
               review_status: str = "candidate_entity_match") -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,published_at=COALESCE(excluded.published_at,discovered_metadata.published_at),
        company_id=excluded.company_id,matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (source_id, canonicalize(url), title, published_at, "wuxi_apptec",
         config["entities"]["wuxi_apptec"]["canonical_name"], document_type,
         "America/New_York", review_status))


def fetch_search_response(db, source, user_agent: str, url: str, title: str) -> bytes | None:
    source_id = source.source_id
    if active_cooldown(db, source_id):
        return None
    host = urlparse(url).hostname or ""
    reason = blocked_host(db, source_id, host)
    if reason:
        set_source_state(db, source_id, 1, "manual_review", reason)
        return None
    session = session_for(source, user_agent)
    timeout = int(source.profile["request_timeout_seconds"])
    interval = float(source.profile["min_interval_seconds"])
    gate = lambda target: reserve_host_slot(
        db, urlparse(target).hostname or "", source_id, interval)
    allowed, note = robot_allowed(session, url, user_agent, timeout, gate, db)
    audit({"source_id": source_id, "outcome": note, "url": url,
           "route": source.route, "allowed": allowed})
    if not allowed:
        block_host(db, source_id, host, note)
        set_source_state(db, source_id, 1, "manual_review", note)
        return None
    gate(url)
    started = time.monotonic()
    try:
        response = session.get(url, headers={"Quartz-Request-Key": REQUEST_KEY},
                               timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        error = f"{type(exc).__name__}: {str(exc)[:180]}"
        set_source_state(db, source_id, 1, "cooldown", error, 300)
        audit({"source_id": source_id, "outcome": "transport_error", "url": url,
               "route": source.route, "error": error, "cooldown_seconds": 300})
        return None
    elapsed = round((time.monotonic() - started) * 1000)
    event = {"source_id": source_id, "url": url, "final_url": response.url,
             "http_status": response.status_code, "route": source.route,
             "elapsed_ms": elapsed}
    if response.status_code in (401, 403):
        block_host(db, source_id, host, f"HTTP {response.status_code}")
        set_source_state(db, source_id, 1, "manual_review", f"HTTP {response.status_code}")
        audit({**event, "outcome": "manual_review_access_control"})
        return None
    if response.status_code == 429:
        set_source_state(db, source_id, 1, "cooldown", "HTTP 429", 3600)
        audit({**event, "outcome": "rate_limited", "cooldown_seconds": 3600})
        return None
    if response.status_code >= 500:
        set_source_state(db, source_id, 1, "cooldown", f"HTTP {response.status_code}", 300)
        audit({**event, "outcome": "http_error", "cooldown_seconds": 300})
        return None
    if response.status_code != 200:
        set_source_state(db, source_id, 1, "manual_review", f"HTTP {response.status_code}")
        audit({**event, "outcome": "http_error", "retry_scheduled": False})
        return None
    marker = challenge_marker(response.content)
    if marker:
        block_host(db, source_id, host, f"challenge marker {marker} detected")
        set_source_state(db, source_id, 1, "manual_review", f"challenge marker {marker} detected")
        audit({**event, "outcome": "manual_review_challenge", "challenge_marker": marker})
        return None
    digest, raw_path = save_raw(source_id, response, response.content)
    record_document(db, source, url, response, digest, raw_path, title, "search_response")
    set_source_state(db, source_id, 0, "closed", None)
    audit({**event, "outcome": "success", "content_hash": digest,
           "raw_path": raw_path, "bytes": len(response.content), "title": title})
    return response.content


def suspend_source(db, source_id: str, reason: str) -> int:
    changed = 0
    for table in ("fierce_search_jobs", "fierce_detail_jobs"):
        changed += db.execute(f"""UPDATE {table} SET status='manual_review',last_error=?
            WHERE source_id=? AND status='pending'""", (reason, source_id)).rowcount
    db.execute("UPDATE fierce_search_state SET last_error=? WHERE source_id=?",
               (reason, source_id))
    db.commit()
    if changed:
        audit({"source_id": source_id, "outcome": "fierce_search_suspended_manual_review",
               "affected_jobs": changed, "error": reason})
    return changed


def process_one(contact: str, now: datetime | None = None) -> int:
    config, source_list = load_sources(
        ROOT / "config" / "sources.yaml", set(SOURCE_IDS), None)
    sources = {source.source_id: source for source in source_list}
    if not sources:
        return 0
    db = connect()
    ensure_schema(db)
    runnable: set[str] = set()
    for source_id, source in sources.items():
        host = SITE[source_id][0]
        state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                           (source_id,)).fetchone()
        reason = blocked_host(db, source_id, host)
        if reason or (state and state[0] == "manual_review"):
            suspend_source(db, source_id, reason or state[1] or "source requires manual review")
        else:
            runnable.add(source_id)
    current = now or datetime.now(timezone.utc)
    ensure_due_runs(db, current, runnable)
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    jobs = db.execute("""SELECT job_id,run_id,source_id,page,mode,attempts
        FROM fierce_search_jobs WHERE status='pending'
        ORDER BY CASE mode WHEN 'backfill' THEN 0 ELSE 1 END,source_id,page""").fetchall()
    job = next((row for row in jobs if not active_cooldown(db, row[2])), None)
    if job:
        job_id, run_id, source_id, page, mode, attempts = job
        source = sources[source_id]
        url = search_url(source_id, page)
        title = f"{source.raw['name']} exact search: {TERM}, page {page}"
        store_hint(db, config, source_id, url, title, None,
                   "search_response", "search_response")
        db.commit()
        body = fetch_search_response(db, source, user_agent, url, title)
        try:
            if body is None:
                raise ValueError("Fierce search response was not saved")
            payload = json.loads(body)
            host, domain_id = SITE[source_id]
            total, dates, results = result_items(payload, TERM, host, domain_id)
            observed = current.isoformat(timespec="seconds")
            start = str(source.raw["backfill_start"])
            skipped_old = 0
            exact_in_window = 0
            for candidate, item_title, published_at, document_type in results:
                if published_at < start:
                    skipped_old += 1
                    continue
                exact_in_window += 1
                store_hint(db, config, source_id, candidate, item_title,
                           published_at, document_type)
                db.execute("""INSERT INTO fierce_detail_jobs
                    (source_id,url,company_id,title,published_at,document_type,mode,first_seen_at)
                    VALUES(?,?,'wuxi_apptec',?,?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET
                    title=excluded.title,published_at=excluded.published_at,
                    document_type=excluded.document_type""",
                    (source_id, candidate, item_title, published_at,
                     document_type, mode, observed))
            raw_has_in_window = any(value >= start for value in dates)
            has_next = (page * PAGE_SIZE < total and len(payload["data"]) == PAGE_SIZE
                        and raw_has_in_window)
            if has_next and page >= MAX_PAGES:
                raise ValueError("Fierce search still has in-window results at page guard")
            if has_next:
                next_page = page + 1
                db.execute("""INSERT OR IGNORE INTO fierce_search_jobs
                    (job_id,run_id,source_id,page,mode) VALUES(?,?,?,?,?)""",
                    (f"{run_id}:{next_page}", run_id, source_id, next_page, mode))
            db.execute("""UPDATE fierce_search_jobs SET status='done',attempts=?,
                skipped_before_window=?,last_error=NULL,processed_at=? WHERE job_id=?""",
                (attempts + 1, skipped_old, observed, job_id))
            remaining = db.execute("""SELECT 1 FROM fierce_search_jobs
                WHERE run_id=? AND status!='done' LIMIT 1""", (run_id,)).fetchone()
            if not remaining:
                pages = db.execute("SELECT count(*) FROM fierce_search_jobs WHERE run_id=?",
                                   (run_id,)).fetchone()[0]
                skipped = db.execute("""SELECT COALESCE(SUM(skipped_before_window),0)
                    FROM fierce_search_jobs WHERE run_id=?""", (run_id,)).fetchone()[0]
                exact = db.execute("SELECT count(*) FROM fierce_detail_jobs WHERE source_id=?",
                                   (source_id,)).fetchone()[0]
                db.execute("""UPDATE fierce_search_state SET last_completed_at=?,
                    total_results=?,pages_scanned=?,exact_results=?,skipped_before_window=?,
                    last_error=NULL WHERE source_id=?""",
                    (observed, total, pages, exact, skipped, source_id))
            db.commit()
            audit({"source_id": source_id, "outcome": "fierce_search_success",
                   "job_id": job_id, "page": page, "reported_total": total,
                   "page_results": len(payload["data"]), "exact_in_window": exact_in_window,
                   "skipped_before_window": skipped_old,
                   "continued": has_next})
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            state = db.execute("SELECT state FROM source_state WHERE source_id=?",
                               (source_id,)).fetchone()
            status = "manual_review" if (state and state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
            db.execute("""UPDATE fierce_search_jobs SET status=?,attempts=?,last_error=?
                WHERE job_id=?""", (status, attempts + 1, str(exc)[:300], job_id))
            db.execute("UPDATE fierce_search_state SET last_error=? WHERE source_id=?",
                       (str(exc)[:300], source_id))
            db.commit()
            audit({"source_id": source_id, "outcome": "fierce_search_error",
                   "job_id": job_id, "error": str(exc)[:300]})
        db.close()
        return 1

    details = db.execute("""SELECT source_id,url,published_at,document_type,attempts
        FROM fierce_detail_jobs WHERE status='pending'
        ORDER BY published_at DESC,source_id,url""").fetchall()
    detail = next((row for row in details if row[0] in runnable and
                   not active_cooldown(db, row[0])), None)
    if not detail:
        db.close()
        return 0
    source_id, url, published_at, document_type, attempts = detail
    source = sources[source_id]
    collect_source(db, source, user_agent, 1, False, url)
    saved = db.execute("SELECT status,raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                       (source_id, canonicalize(url))).fetchone()
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (source_id,)).fetchone()
    if saved and saved[0] == 200 and saved[1] and state and state[0] == "closed":
        processed = current.isoformat(timespec="seconds")
        db.execute("""UPDATE fierce_detail_jobs SET status='done',attempts=?,
            last_error=NULL,processed_at=? WHERE source_id=? AND url=?""",
            (attempts + 1, processed, source_id, url))
        db.commit()
        audit({"source_id": source_id, "outcome": "fierce_detail_success",
               "url": url, "published_at": published_at,
               "document_type": document_type})
    else:
        error = state[1] if state and state[1] else "Fierce detail response was not saved"
        status = "manual_review" if (state and state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("""UPDATE fierce_detail_jobs SET status=?,attempts=?,last_error=?
            WHERE source_id=? AND url=?""",
            (status, attempts + 1, error[:300], source_id, url))
        db.commit()
        audit({"source_id": source_id, "outcome": "fierce_detail_error",
               "url": url, "error": error[:300]})
    db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    print(f"processed={process_one(contact)}")
