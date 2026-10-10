#!/usr/bin/env python3
"""Low-volume exact-entity discovery through CNStock's public search API."""
from __future__ import annotations

import html
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlparse

import requests

from collector.collect import (
    ROOT, active_cooldown, audit, block_host, blocked_host, canonicalize,
    challenge_marker, collect_source, connect, load_sources, record_document,
    reserve_host_slot, robot_allowed, save_raw, session_for, set_source_state,
)

SOURCE_ID = "shanghai_securities_news"
API_HOST = "api.cnstock.com"
DETAIL_HOST = "www.cnstock.com"
ENDPOINT = "https://api.cnstock.com/search/news"
PAGE_SIZE = 10
MAX_PAGES = 200
TERMS = {
    "cxmt": ("cxmt", "长鑫科技"),
    "wuxi_apptec": ("wuxi_apptec", "药明康德"),
}


@dataclass(frozen=True)
class SearchItem:
    url: str
    title: str
    summary: str
    content: str
    published_at: str
    document_type: str


def plain(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(
        re.sub(r"<[^>]+>", "", str(value or "")))).strip().replace("\u200b", "")


def landing_url(term: str) -> str:
    return f"https://www.cnstock.com/searchResult?word={quote(term)}"


def evidence_url(term: str, page: int) -> str:
    return ENDPOINT + "?" + urlencode({
        "type": "0", "word": term, "activeKey": "0", "pageNum": page,
    })


def item_date(item: dict) -> str | None:
    info = (item.get("shareInfo") or {}).get("dateInfo")
    if isinstance(info, dict):
        try:
            return date(int(info["year"]), int(info["month"]), int(info["day"])).isoformat()
        except (KeyError, TypeError, ValueError):
            pass
    match = re.search(r"([12]\d{3})[-/.](\d{1,2})[-/.](\d{1,2})",
                      str(item.get("pubTime") or ""))
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def parse_results(body: bytes) -> tuple[int, list[SearchItem], bool, list[str]]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("CNStock search response is not valid JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("code") != 200 or not isinstance(data, dict):
        raise ValueError("CNStock search response has invalid code/data")
    results = data.get("list")
    if not isinstance(results, list) or len(results) > 20:
        raise ValueError("CNStock search response has invalid or oversized list")
    try:
        total = int(data.get("total") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("CNStock search response has invalid total") from exc
    if total < 0:
        raise ValueError("CNStock search response has negative total")
    items: list[SearchItem] = []
    observed_dates: list[str] = []
    seen: set[str] = set()
    for raw in results:
        if not isinstance(raw, dict):
            continue
        published_at = item_date(raw)
        if published_at:
            observed_dates.append(published_at)
        cont_id = str(raw.get("contId") or "")
        if raw.get("forwardType") != 4 or not re.fullmatch(r"\d{1,12}", cont_id):
            continue
        url = canonicalize(f"https://www.cnstock.com/commonDetail/{cont_id}")
        if url in seen or urlparse(url).hostname != DETAIL_HOST:
            continue
        if not published_at:
            continue
        node_name = plain((raw.get("nodeInfo") or {}).get("name"))
        document_type = "news_brief" if node_name == "快讯" else "independent_news"
        items.append(SearchItem(
            url=url,
            title=plain(raw.get("name")) or f"CNStock article {cont_id}",
            summary=plain(raw.get("summary")),
            content=plain(raw.get("content")),
            published_at=published_at,
            document_type=document_type,
        ))
        seen.add(url)
    return total, items, bool(data.get("hasNext")), observed_dates


def exact_results(items: list[SearchItem], term: str) -> list[SearchItem]:
    return [item for item in items
            if term in f"{item.title} {item.summary} {item.content}"]


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS cnstock_search_state (
        term_id TEXT PRIMARY KEY,company_id TEXT NOT NULL,search_term TEXT NOT NULL,
        last_completed_at TEXT,total_results INTEGER,pages_scanned INTEGER NOT NULL DEFAULT 0,
        exact_results INTEGER NOT NULL DEFAULT 0,
        skipped_before_window INTEGER NOT NULL DEFAULT 0,
        coverage_note TEXT NOT NULL DEFAULT 'not_started',last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS cnstock_search_jobs (
        job_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,term_id TEXT NOT NULL,page INTEGER NOT NULL,
        mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,skipped_before_window INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,processed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS cnstock_detail_jobs (
        url TEXT PRIMARY KEY,term_id TEXT NOT NULL,company_id TEXT NOT NULL,title TEXT NOT NULL,
        summary TEXT,published_at TEXT NOT NULL,document_type TEXT NOT NULL,
        mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,processed_at TEXT,first_seen_at TEXT NOT NULL)""")
    for term_id, (company_id, term) in TERMS.items():
        db.execute("""INSERT INTO cnstock_search_state(term_id,company_id,search_term)
            VALUES(?,?,?) ON CONFLICT(term_id) DO UPDATE SET
            company_id=excluded.company_id,search_term=excluded.search_term""",
                   (term_id, company_id, term))
    db.commit()


def ensure_due_runs(db: sqlite3.Connection, now: datetime) -> None:
    cutoff = now - timedelta(days=1)
    for term_id, last_completed in db.execute(
            "SELECT term_id,last_completed_at FROM cnstock_search_state ORDER BY term_id"):
        unfinished = db.execute("""SELECT 1 FROM cnstock_search_jobs WHERE term_id=?
            AND status NOT IN ('done','manual_review') LIMIT 1""", (term_id,)).fetchone()
        if unfinished or (last_completed and datetime.fromisoformat(last_completed) > cutoff):
            continue
        mode = "backfill" if not last_completed else "incremental"
        run_id = f"{mode}:{term_id}:{now.date().isoformat()}"
        db.execute("""INSERT OR IGNORE INTO cnstock_search_jobs
            (job_id,run_id,term_id,page,mode) VALUES(?,?,?,?,?)""",
            (f"{run_id}:1", run_id, term_id, 1, mode))
        db.execute("UPDATE cnstock_search_state SET coverage_note='in_progress' WHERE term_id=?",
                   (term_id,))
    db.commit()


def store_hint(db: sqlite3.Connection, config: dict, company_id: str, url: str,
               title: str, published_at: str | None, document_type: str,
               review_status: str) -> None:
    is_query = review_status == "search_response"
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id,canonical_url) DO UPDATE SET title=excluded.title,
        published_at=COALESCE(excluded.published_at,discovered_metadata.published_at),
        company_id=excluded.company_id,matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, published_at,
         None if is_query else company_id,
         None if is_query else config["entities"][company_id]["canonical_name"],
         document_type, "Asia/Shanghai", review_status))


def fetch_search_response(db: sqlite3.Connection, source, user_agent: str,
                          url: str, term: str, page: int) -> bytes | None:
    if active_cooldown(db, SOURCE_ID):
        return None
    reason = blocked_host(db, SOURCE_ID, API_HOST)
    if reason:
        set_source_state(db, SOURCE_ID, 1, "manual_review", reason)
        return None
    session = session_for(source, user_agent)
    timeout = int(source.profile["request_timeout_seconds"])
    interval = float(source.profile["min_interval_seconds"])
    gate = lambda target: reserve_host_slot(
        db, urlparse(target).hostname or "", SOURCE_ID, interval)
    allowed, note = robot_allowed(session, url, user_agent, timeout, gate, db)
    audit({"source_id": SOURCE_ID, "outcome": note, "url": url,
           "route": source.route, "allowed": allowed})
    if not allowed:
        block_host(db, SOURCE_ID, API_HOST, note)
        set_source_state(db, SOURCE_ID, 1, "manual_review", note)
        return None
    gate(ENDPOINT)
    started = time.monotonic()
    headers = {
        "Accept": "application/json",
        "cnstock-client-type": "01",
        "Origin": "https://www.cnstock.com",
        "Referer": landing_url(term),
    }
    try:
        response = session.post(ENDPOINT, json={
            "type": "0", "word": term, "activeKey": "0", "pageNum": page,
        }, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        error = f"{type(exc).__name__}: {str(exc)[:180]}"
        set_source_state(db, SOURCE_ID, 1, "cooldown", error, 300)
        audit({"source_id": SOURCE_ID, "outcome": "transport_error", "url": url,
               "route": source.route, "error": error, "cooldown_seconds": 300})
        return None
    event = {"source_id": SOURCE_ID, "url": url, "final_url": response.url,
             "http_status": response.status_code, "route": source.route,
             "elapsed_ms": round((time.monotonic() - started) * 1000)}
    if response.status_code in (401, 403):
        block_host(db, SOURCE_ID, API_HOST, f"HTTP {response.status_code}")
        set_source_state(db, SOURCE_ID, 1, "manual_review", f"HTTP {response.status_code}")
        audit({**event, "outcome": "manual_review_access_control"})
        return None
    if response.status_code == 429:
        set_source_state(db, SOURCE_ID, 1, "cooldown", "HTTP 429", 3600)
        audit({**event, "outcome": "rate_limited", "cooldown_seconds": 3600})
        return None
    if response.status_code >= 500:
        set_source_state(db, SOURCE_ID, 1, "cooldown", f"HTTP {response.status_code}", 300)
        audit({**event, "outcome": "http_error", "cooldown_seconds": 300})
        return None
    if response.status_code != 200:
        set_source_state(db, SOURCE_ID, 1, "manual_review", f"HTTP {response.status_code}")
        audit({**event, "outcome": "http_error", "retry_scheduled": False})
        return None
    marker = challenge_marker(response.content)
    if marker:
        reason = f"challenge marker {marker} detected"
        block_host(db, SOURCE_ID, API_HOST, reason)
        set_source_state(db, SOURCE_ID, 1, "manual_review", reason)
        audit({**event, "outcome": "manual_review_challenge", "challenge_marker": marker})
        return None
    digest, raw_path = save_raw(SOURCE_ID, response, response.content)
    title = f"上海证券报精确检索：{term} 第{page}页"
    record_document(db, source, url, response, digest, raw_path, title, "search_response")
    set_source_state(db, SOURCE_ID, 0, "closed", None)
    audit({**event, "outcome": "success", "content_hash": digest,
           "raw_path": raw_path, "bytes": len(response.content), "title": title})
    return response.content


def suspend_pending(db: sqlite3.Connection, reason: str) -> None:
    changed = 0
    for table in ("cnstock_search_jobs", "cnstock_detail_jobs"):
        changed += db.execute(
            f"UPDATE {table} SET status='manual_review',last_error=? WHERE status='pending'",
            (reason,)).rowcount
    db.execute("UPDATE cnstock_search_state SET last_error=?", (reason,))
    db.commit()
    if changed:
        audit({"source_id": SOURCE_ID, "outcome": "cnstock_search_suspended_manual_review",
               "affected_jobs": changed, "error": reason})


def focus_company(config: dict) -> str | None:
    focus = (config.get("collection_defaults", {}).get("scheduling", {})
             .get("focus_company"))
    return focus if focus in TERMS else None


def next_detail_job(db: sqlite3.Connection, focus: str | None = None):
    """Pick the least-served entity before recency among queued detail pages."""
    return db.execute("""SELECT j.url,j.term_id,j.company_id,j.title,j.published_at,
        j.document_type,j.attempts FROM cnstock_detail_jobs j
        LEFT JOIN (SELECT term_id,count(*) AS completed FROM cnstock_detail_jobs
                   WHERE status='done' GROUP BY term_id) progress ON progress.term_id=j.term_id
        WHERE j.status='pending'
        ORDER BY CASE WHEN j.term_id=? THEN 0 ELSE 1 END,
                 COALESCE(progress.completed,0),j.published_at DESC,j.url LIMIT 1""",
        (focus or "",)).fetchone()


def process_detail(db: sqlite3.Connection, source, config: dict,
                   contact: str, current: datetime) -> bool:
    job = next_detail_job(db, focus_company(config))
    if not job:
        return False
    url, _term_id, company_id, title, published_at, document_type, attempts = job
    store_hint(db, config, company_id, url, title, published_at,
               document_type, "candidate_entity_match")
    db.commit()
    collect_source(db, source, f"ListedCompanyEventIntelligence/0.1 (+{contact})",
                   1, False, url)
    saved = db.execute("""SELECT status,raw_path FROM documents
        WHERE source_id=? AND canonical_url=?""",
                       (SOURCE_ID, canonicalize(url))).fetchone()
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    if saved and saved[0] == 200 and saved[1] and state and state[0] == "closed":
        db.execute("""UPDATE cnstock_detail_jobs SET status='done',attempts=?,
            last_error=NULL,processed_at=? WHERE url=?""",
                   (attempts + 1, current.isoformat(timespec="seconds"), url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "cnstock_detail_success",
               "url": url, "published_at": published_at,
               "document_type": document_type})
    else:
        error = state[1] if state and state[1] else "CNStock detail response was not saved"
        status = "manual_review" if (state and state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("UPDATE cnstock_detail_jobs SET status=?,attempts=?,last_error=? WHERE url=?",
                   (status, attempts + 1, error[:300], url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "cnstock_detail_error",
               "url": url, "error": error[:300]})
    return True


def next_search_job(db: sqlite3.Connection, focus: str | None = None):
    return db.execute("""SELECT j.job_id,j.run_id,j.term_id,j.page,j.mode,j.attempts,
        s.company_id,s.search_term FROM cnstock_search_jobs j JOIN cnstock_search_state s
        ON s.term_id=j.term_id WHERE j.status='pending'
        ORDER BY CASE WHEN j.term_id=? THEN 0 ELSE 1 END,
                 CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,
                 s.pages_scanned,j.term_id,j.page LIMIT 1""", (focus or "",)).fetchone()


def should_prioritize_search(db: sqlite3.Connection, focus: str | None = None) -> bool:
    """Alternate queued result pages with detail fetches while a search remains.

    Detail candidates may substantially outnumber result pages.  Alternating
    the two work types prevents that detail backlog from starving the less
    scanned entity's next public result page, without raising request cadence.
    """
    if not next_search_job(db, focus):
        return False
    last_search = db.execute("""SELECT MAX(processed_at) FROM cnstock_search_jobs
        WHERE status='done'""").fetchone()[0]
    last_detail = db.execute("""SELECT MAX(processed_at) FROM cnstock_detail_jobs
        WHERE status='done'""").fetchone()[0]
    return not last_search or (last_detail is not None and last_detail >= last_search)


def process_search(db: sqlite3.Connection, source, config: dict,
                   contact: str, current: datetime) -> bool:
    job = next_search_job(db, focus_company(config))
    if not job:
        return False
    job_id, run_id, term_id, page, mode, attempts, company_id, term = job
    url = evidence_url(term, page)
    store_hint(db, config, company_id, url, f"上海证券报精确检索：{term} 第{page}页",
               None, "search_response", "search_response")
    db.commit()
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    body = fetch_search_response(db, source, user_agent, url, term, page)
    try:
        if body is None:
            raise ValueError("CNStock search response was not saved")
        total, items, has_next, observed_dates = parse_results(body)
        exact = exact_results(items, term)
        observed = current.isoformat(timespec="seconds")
        known = {row[0] for row in db.execute(
            "SELECT url FROM cnstock_detail_jobs WHERE term_id=?", (term_id,))}
        known_boundary = mode == "incremental" and any(item.url in known for item in exact)
        skipped_old = 0
        exact_in_window = 0
        for item in exact:
            if item.published_at < str(source.raw["backfill_start"]):
                skipped_old += 1
                continue
            exact_in_window += 1
            store_hint(db, config, company_id, item.url, item.title, item.published_at,
                       item.document_type, "candidate_entity_match")
            db.execute("""INSERT INTO cnstock_detail_jobs
                (url,term_id,company_id,title,summary,published_at,document_type,mode,first_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                summary=excluded.summary,published_at=excluded.published_at,
                document_type=excluded.document_type""",
                (item.url, term_id, company_id, item.title, item.summary,
                 item.published_at, item.document_type, mode, observed))
        date_boundary = bool(observed_dates and
                             min(observed_dates) < str(source.raw["backfill_start"]))
        guard_boundary = page >= MAX_PAGES
        continue_pages = (has_next and not date_boundary and
                          not known_boundary and not guard_boundary)
        if continue_pages:
            db.execute("""INSERT OR IGNORE INTO cnstock_search_jobs
                (job_id,run_id,term_id,page,mode) VALUES(?,?,?,?,?)""",
                (f"{run_id}:{page + 1}", run_id, term_id, page + 1, mode))
        db.execute("""UPDATE cnstock_search_jobs SET status='done',attempts=?,
            skipped_before_window=?,last_error=NULL,processed_at=? WHERE job_id=?""",
            (attempts + 1, skipped_old, observed, job_id))
        pages = db.execute("""SELECT count(*) FROM cnstock_search_jobs
            WHERE run_id=? AND status='done'""", (run_id,)).fetchone()[0]
        skipped = db.execute("""SELECT COALESCE(SUM(skipped_before_window),0)
            FROM cnstock_search_jobs WHERE run_id=?""", (run_id,)).fetchone()[0]
        exact_count = db.execute(
            "SELECT count(*) FROM cnstock_detail_jobs WHERE term_id=?", (term_id,)).fetchone()[0]
        db.execute("""UPDATE cnstock_search_state SET total_results=?,pages_scanned=?,
            exact_results=?,skipped_before_window=?,last_error=NULL WHERE term_id=?""",
            (total, pages, exact_count, skipped, term_id))
        if not continue_pages:
            coverage = ("incremental_known_boundary" if known_boundary else
                        "date_boundary_reached" if date_boundary else
                        "max_pages_guard_reached" if guard_boundary else
                        "result_exhausted")
            db.execute("""UPDATE cnstock_search_state SET last_completed_at=?,coverage_note=?
                WHERE term_id=?""", (observed, coverage, term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "cnstock_search_success",
               "job_id": job_id, "term_id": term_id, "page": page,
               "total_results": total, "page_results": len(items),
               "exact_in_window": exact_in_window,
               "skipped_before_window": skipped_old, "continued": continue_pages})
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        state = db.execute("SELECT state FROM source_state WHERE source_id=?",
                           (SOURCE_ID,)).fetchone()
        status = "manual_review" if (state and state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("UPDATE cnstock_search_jobs SET status=?,attempts=?,last_error=? WHERE job_id=?",
                   (status, attempts + 1, str(exc)[:300], job_id))
        db.execute("UPDATE cnstock_search_state SET last_error=? WHERE term_id=?",
                   (str(exc)[:300], term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "cnstock_search_error",
               "job_id": job_id, "error": str(exc)[:300]})
    return True


def process_one(contact: str, now: datetime | None = None) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    reason = (blocked_host(db, SOURCE_ID, API_HOST) or
              blocked_host(db, SOURCE_ID, DETAIL_HOST))
    if reason or (state and state[0] == "manual_review"):
        suspend_pending(db, reason or state[1] or "source requires manual review")
        db.close()
        return 0
    if active_cooldown(db, SOURCE_ID):
        db.close()
        return 0
    current = now or datetime.now(timezone.utc)
    ensure_due_runs(db, current)
    focus = focus_company(config)
    drain_details = bool(config.get("collection_defaults", {}).get("scheduling", {})
                         .get("drain_discovered_details_first", False))
    if drain_details:
        processed = (process_detail(db, source, config, contact, current) or
                     process_search(db, source, config, contact, current))
    elif should_prioritize_search(db, focus):
        processed = (process_search(db, source, config, contact, current) or
                     process_detail(db, source, config, contact, current))
    else:
        processed = (process_detail(db, source, config, contact, current) or
                     process_search(db, source, config, contact, current))
    db.close()
    return int(processed)


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    print(f"processed={process_one(contact)}")
