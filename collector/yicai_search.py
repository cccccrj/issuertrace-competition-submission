#!/usr/bin/env python3
"""Low-volume exact-entity discovery through Yicai's public web search flow."""
from __future__ import annotations

import html
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

import requests

from collector.collect import (
    ROOT, active_cooldown, audit, block_host, blocked_host, canonicalize,
    challenge_marker, collect_source, connect, load_sources, record_document,
    reserve_host_slot, robot_allowed, save_raw, session_for, set_source_state,
)

SOURCE_ID = "yicai"
HOST = "www.yicai.com"
PAGE_SIZE = 20
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
    published_at: str


def plain(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(
        re.sub(r"<[^>]+>", "", str(value or "")))).strip().replace("\u200b", "")


def landing_url(term: str) -> str:
    return f"https://{HOST}/search?keys={quote(term)}"


def data_url(term: str, page: int) -> str:
    return (f"https://{HOST}/api/ajax/getSearchResult?page={page}&"
            f"pagesize={PAGE_SIZE}&keys={quote(term)}")


def infer_date(value: str, anchor: date) -> date | None:
    full = re.search(r"([12]\d{3})[-/]([01]?\d)[-/]([0-3]?\d)", value)
    short = re.search(r"(?<!\d)([01]?\d)-([0-3]?\d)(?:\s|$)", value)
    try:
        if full:
            return date(*(int(part) for part in full.groups()))
        if not short:
            return None
        candidate = date(anchor.year, int(short.group(1)), int(short.group(2)))
        if candidate > anchor + timedelta(days=31):
            candidate = date(anchor.year - 1, candidate.month, candidate.day)
        return candidate
    except ValueError:
        return None


def parse_results(body: bytes, anchor_date: str) -> tuple[list[SearchItem], list[str], int, bool]:
    try:
        payload = json.loads(body)
        anchor = date.fromisoformat(anchor_date)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Yicai search response is not valid JSON/date") from exc
    results = payload.get("results")
    if payload.get("status") != 1 or not isinstance(results, dict) or not isinstance(results.get("docs"), list):
        raise ValueError("Yicai search response has invalid status/results")
    docs = results["docs"]
    if len(docs) > PAGE_SIZE:
        raise ValueError("Yicai search page exceeds 20-result guard")
    items: list[SearchItem] = []
    observed_dates: list[str] = []
    seen: set[str] = set()
    rolling_anchor = anchor
    for raw in docs:
        if not isinstance(raw, dict):
            continue
        found_date = infer_date(plain(raw.get("creationDate")), rolling_anchor)
        if found_date:
            rolling_anchor = found_date
            observed_dates.append(found_date.isoformat())
        raw_url = plain(raw.get("url"))
        url = canonicalize(urljoin(f"https://{HOST}/", raw_url))
        parsed = urlparse(url)
        if (not found_date or parsed.scheme != "https" or parsed.hostname != HOST or
                not re.fullmatch(r"/news/\d+\.html", parsed.path) or url in seen):
            continue
        items.append(SearchItem(url, plain(raw.get("title")), plain(raw.get("desc")),
                                found_date.isoformat()))
        seen.add(url)
    return items, observed_dates, len(docs), bool(docs)


def exact_results(items: list[SearchItem], term: str) -> list[SearchItem]:
    return [item for item in items if term in f"{item.title} {item.summary}"]


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS yicai_search_state (
        term_id TEXT PRIMARY KEY,company_id TEXT NOT NULL,search_term TEXT NOT NULL,
        last_completed_at TEXT,pages_scanned INTEGER NOT NULL DEFAULT 0,
        exact_results INTEGER NOT NULL DEFAULT 0,
        skipped_before_window INTEGER NOT NULL DEFAULT 0,
        coverage_note TEXT NOT NULL DEFAULT 'not_started',last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS yicai_search_jobs (
        job_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,term_id TEXT NOT NULL,page INTEGER NOT NULL,
        anchor_date TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,skipped_before_window INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,processed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS yicai_detail_jobs (
        url TEXT PRIMARY KEY,term_id TEXT NOT NULL,company_id TEXT NOT NULL,title TEXT NOT NULL,
        summary TEXT,published_at TEXT NOT NULL,document_type TEXT NOT NULL DEFAULT 'independent_news',
        mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,processed_at TEXT,first_seen_at TEXT NOT NULL)""")
    for term_id, (company_id, term) in TERMS.items():
        db.execute("""INSERT INTO yicai_search_state(term_id,company_id,search_term)
            VALUES(?,?,?) ON CONFLICT(term_id) DO UPDATE SET
            company_id=excluded.company_id,search_term=excluded.search_term""",
                   (term_id, company_id, term))
    db.commit()


def ensure_due_runs(db: sqlite3.Connection, now: datetime) -> None:
    cutoff = now - timedelta(days=1)
    for term_id, last_completed in db.execute(
            "SELECT term_id,last_completed_at FROM yicai_search_state ORDER BY term_id"):
        unfinished = db.execute("""SELECT 1 FROM yicai_search_jobs WHERE term_id=?
            AND status NOT IN ('done','manual_review') LIMIT 1""", (term_id,)).fetchone()
        if unfinished or (last_completed and datetime.fromisoformat(last_completed) > cutoff):
            continue
        mode = "backfill" if not last_completed else "incremental"
        run_id = f"{mode}:{term_id}:{now.date().isoformat()}"
        db.execute("""INSERT OR IGNORE INTO yicai_search_jobs
            (job_id,run_id,term_id,page,anchor_date,mode) VALUES(?,?,?,?,?,?)""",
            (f"{run_id}:0", run_id, term_id, 0, now.date().isoformat(), mode))
        db.execute("UPDATE yicai_search_state SET coverage_note='in_progress' WHERE term_id=?",
                   (term_id,))
    db.commit()


def store_hint(db: sqlite3.Connection, config: dict, company_id: str, url: str,
               title: str, published_at: str | None, document_type: str,
               review_status: str) -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id,canonical_url) DO UPDATE SET title=excluded.title,
        published_at=COALESCE(excluded.published_at,discovered_metadata.published_at),
        company_id=excluded.company_id,matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, published_at,
         None if review_status == "search_response" else company_id,
         None if review_status == "search_response" else config["entities"][company_id]["canonical_name"],
         document_type, "Asia/Shanghai", review_status))


def fetch_evidence(db: sqlite3.Connection, source, session: requests.Session,
                   user_agent: str, url: str, title: str,
                   headers: dict[str, str] | None = None) -> bytes | None:
    if active_cooldown(db, SOURCE_ID):
        return None
    host = urlparse(url).hostname or ""
    reason = blocked_host(db, SOURCE_ID, host)
    if reason:
        set_source_state(db, SOURCE_ID, 1, "manual_review", reason)
        return None
    timeout = int(source.profile["request_timeout_seconds"])
    interval = float(source.profile["min_interval_seconds"])
    gate = lambda target: reserve_host_slot(db, urlparse(target).hostname or "", SOURCE_ID, interval)
    allowed, note = robot_allowed(session, url, user_agent, timeout, gate, db)
    audit({"source_id": SOURCE_ID, "outcome": note, "url": url,
           "route": source.route, "allowed": allowed})
    if not allowed:
        block_host(db, SOURCE_ID, host, note)
        set_source_state(db, SOURCE_ID, 1, "manual_review", note)
        return None
    gate(url)
    started = time.monotonic()
    try:
        response = session.get(url, headers=headers or {}, timeout=timeout, allow_redirects=True)
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
        block_host(db, SOURCE_ID, host, f"HTTP {response.status_code}")
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
        block_host(db, SOURCE_ID, host, f"challenge marker {marker} detected")
        set_source_state(db, SOURCE_ID, 1, "manual_review", f"challenge marker {marker} detected")
        audit({**event, "outcome": "manual_review_challenge", "challenge_marker": marker})
        return None
    digest, raw_path = save_raw(SOURCE_ID, response, response.content)
    record_document(db, source, url, response, digest, raw_path, title, "search_response")
    set_source_state(db, SOURCE_ID, 0, "closed", None)
    audit({**event, "outcome": "success", "content_hash": digest,
           "raw_path": raw_path, "bytes": len(response.content), "title": title})
    return response.content


def suspend_pending(db: sqlite3.Connection, reason: str) -> None:
    changed = 0
    for table in ("yicai_search_jobs", "yicai_detail_jobs"):
        changed += db.execute(f"UPDATE {table} SET status='manual_review',last_error=? WHERE status='pending'",
                              (reason,)).rowcount
    db.execute("UPDATE yicai_search_state SET last_error=?", (reason,))
    db.commit()
    if changed:
        audit({"source_id": SOURCE_ID, "outcome": "yicai_search_suspended_manual_review",
               "affected_jobs": changed, "error": reason})


def process_detail(db: sqlite3.Connection, source, config: dict, contact: str,
                   current: datetime) -> bool:
    job = db.execute("""SELECT url,company_id,title,published_at,document_type,attempts
        FROM yicai_detail_jobs WHERE status='pending' ORDER BY published_at DESC,url LIMIT 1""").fetchone()
    if not job:
        return False
    url, company_id, title, published_at, document_type, attempts = job
    store_hint(db, config, company_id, url, title, published_at, document_type,
               "candidate_entity_match")
    db.commit()
    collect_source(db, source, f"ListedCompanyEventIntelligence/0.1 (+{contact})", 1, False, url)
    saved = db.execute("SELECT status,raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                       (SOURCE_ID, canonicalize(url))).fetchone()
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?", (SOURCE_ID,)).fetchone()
    if saved and saved[0] == 200 and saved[1] and state and state[0] == "closed":
        db.execute("""UPDATE yicai_detail_jobs SET status='done',attempts=?,last_error=NULL,
            processed_at=? WHERE url=?""", (attempts + 1, current.isoformat(timespec="seconds"), url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "yicai_detail_success", "url": url,
               "published_at": published_at})
    else:
        error = state[1] if state and state[1] else "Yicai detail response was not saved"
        status = "manual_review" if (state and state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("UPDATE yicai_detail_jobs SET status=?,attempts=?,last_error=? WHERE url=?",
                   (status, attempts + 1, error[:300], url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "yicai_detail_error", "url": url,
               "error": error[:300]})
    return True


def process_search(db: sqlite3.Connection, source, config: dict, contact: str,
                   current: datetime) -> bool:
    job = db.execute("""SELECT j.job_id,j.run_id,j.term_id,j.page,j.anchor_date,j.mode,j.attempts,
        s.company_id,s.search_term FROM yicai_search_jobs j JOIN yicai_search_state s
        ON s.term_id=j.term_id WHERE j.status='pending'
        ORDER BY CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,j.term_id,j.page LIMIT 1""").fetchone()
    if not job:
        return False
    job_id, run_id, term_id, page, anchor_date, mode, attempts, company_id, term = job
    landing, endpoint = landing_url(term), data_url(term, page)
    store_hint(db, config, company_id, landing, f"第一财经检索入口：{term}", None,
               "search_response", "search_response")
    store_hint(db, config, company_id, endpoint, f"第一财经精确检索：{term} 第{page + 1}页", None,
               "search_response", "search_response")
    db.commit()
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    session = session_for(source, user_agent)
    try:
        if fetch_evidence(db, source, session, user_agent, landing,
                          f"第一财经检索入口：{term}") is None:
            raise ValueError("Yicai search bootstrap response was not saved")
        body = fetch_evidence(db, source, session, user_agent, endpoint,
                              f"第一财经精确检索：{term} 第{page + 1}页",
                              {"Referer": landing, "X-Requested-With": "XMLHttpRequest"})
        if body is None:
            raise ValueError("Yicai search data response was not saved")
        items, observed_dates, raw_count, has_data = parse_results(body, anchor_date)
        exact = exact_results(items, term)
        observed = current.isoformat(timespec="seconds")
        known = {row[0] for row in db.execute("SELECT url FROM yicai_detail_jobs WHERE term_id=?", (term_id,))}
        known_boundary = mode == "incremental" and any(item.url in known for item in exact)
        skipped_old = exact_in_window = 0
        for item in exact:
            if item.published_at < str(source.raw["backfill_start"]):
                skipped_old += 1
                continue
            exact_in_window += 1
            store_hint(db, config, company_id, item.url, item.title, item.published_at,
                       "independent_news", "candidate_entity_match")
            db.execute("""INSERT INTO yicai_detail_jobs
                (url,term_id,company_id,title,summary,published_at,mode,first_seen_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                summary=excluded.summary,published_at=excluded.published_at""",
                (item.url, term_id, company_id, item.title, item.summary, item.published_at, mode, observed))
        date_boundary = bool(observed_dates and min(observed_dates) < str(source.raw["backfill_start"]))
        guard_boundary = page + 1 >= MAX_PAGES
        continue_pages = (has_data and raw_count == PAGE_SIZE and not date_boundary and
                          not known_boundary and not guard_boundary)
        if continue_pages:
            next_anchor = min(observed_dates) if observed_dates else anchor_date
            db.execute("""INSERT OR IGNORE INTO yicai_search_jobs
                (job_id,run_id,term_id,page,anchor_date,mode) VALUES(?,?,?,?,?,?)""",
                (f"{run_id}:{page + 1}", run_id, term_id, page + 1, next_anchor, mode))
        db.execute("""UPDATE yicai_search_jobs SET status='done',attempts=?,skipped_before_window=?,
            last_error=NULL,processed_at=? WHERE job_id=?""",
            (attempts + 1, skipped_old, observed, job_id))
        pages = db.execute("SELECT count(*) FROM yicai_search_jobs WHERE run_id=? AND status='done'", (run_id,)).fetchone()[0]
        skipped = db.execute("SELECT COALESCE(SUM(skipped_before_window),0) FROM yicai_search_jobs WHERE run_id=?", (run_id,)).fetchone()[0]
        exact_count = db.execute("SELECT count(*) FROM yicai_detail_jobs WHERE term_id=?", (term_id,)).fetchone()[0]
        db.execute("""UPDATE yicai_search_state SET pages_scanned=?,exact_results=?,
            skipped_before_window=?,last_error=NULL WHERE term_id=?""",
            (pages, exact_count, skipped, term_id))
        if not continue_pages:
            coverage = ("incremental_known_boundary" if known_boundary else
                        "date_boundary_reached" if date_boundary else
                        "max_pages_guard_reached" if guard_boundary else "result_exhausted")
            db.execute("UPDATE yicai_search_state SET last_completed_at=?,coverage_note=? WHERE term_id=?",
                       (observed, coverage, term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "yicai_search_success", "job_id": job_id,
               "term_id": term_id, "page": page, "page_results": len(items),
               "exact_in_window": exact_in_window, "skipped_before_window": skipped_old,
               "continued": continue_pages})
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        state = db.execute("SELECT state FROM source_state WHERE source_id=?", (SOURCE_ID,)).fetchone()
        status = "manual_review" if (state and state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("UPDATE yicai_search_jobs SET status=?,attempts=?,last_error=? WHERE job_id=?",
                   (status, attempts + 1, str(exc)[:300], job_id))
        db.execute("UPDATE yicai_search_state SET last_error=? WHERE term_id=?", (str(exc)[:300], term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "yicai_search_error", "job_id": job_id,
               "error": str(exc)[:300]})
    return True


def process_one(contact: str, now: datetime | None = None) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?", (SOURCE_ID,)).fetchone()
    reason = blocked_host(db, SOURCE_ID, HOST)
    if reason or (state and state[0] == "manual_review"):
        suspend_pending(db, reason or state[1] or "source requires manual review")
        db.close()
        return 0
    if active_cooldown(db, SOURCE_ID):
        db.close()
        return 0
    current = now or datetime.now(timezone.utc)
    ensure_due_runs(db, current)
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
