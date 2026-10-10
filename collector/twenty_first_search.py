#!/usr/bin/env python3
"""Low-volume exact-name search through 21财经's public frontend endpoints."""
from __future__ import annotations

import base64
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from collector.collect import (
    ROOT, active_cooldown, audit, block_host, blocked_host, canonicalize,
    challenge_marker, collect_source, connect, load_sources, record_document,
    reserve_host_slot, robot_allowed, save_raw, session_for, set_source_state,
)

SOURCE_ID = "twenty_first_finance"
CALLBACK = "collector"
KEY_ENDPOINT = "https://so.21jingji.com/elk/search/getSearchKey"
SEARCH_ENDPOINT = "https://so.21jingji.com/elk/search/searchWeb/"
PAGE_SIZE = 20
MAX_PAGES = 5  # the public page disables loading after page 5
IV = b"21jingji_search_"
TERMS = {
    "cxmt_legal": ("cxmt", "长鑫科技集团股份有限公司"),
    "wuxi_legal": ("wuxi_apptec", "无锡药明康德新药开发股份有限公司"),
}


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS twenty_first_search_state (
        term_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, search_term TEXT NOT NULL,
        last_completed_at TEXT, pages_scanned INTEGER, exact_results INTEGER,
        skipped_before_window INTEGER, last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS twenty_first_search_jobs (
        job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, term_id TEXT NOT NULL,
        page INTEGER NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        skipped_before_window INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(twenty_first_search_jobs)")}
    if "skipped_before_window" not in columns:
        db.execute("""ALTER TABLE twenty_first_search_jobs ADD COLUMN
            skipped_before_window INTEGER NOT NULL DEFAULT 0""")
    db.execute("""CREATE TABLE IF NOT EXISTS twenty_first_detail_jobs (
        url TEXT PRIMARY KEY, term_id TEXT NOT NULL, company_id TEXT NOT NULL,
        title TEXT NOT NULL, published_at TEXT NOT NULL, document_type TEXT NOT NULL,
        mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, processed_at TEXT,
        first_seen_at TEXT NOT NULL)""")
    for term_id, (company_id, term) in TERMS.items():
        db.execute("""INSERT INTO twenty_first_search_state(term_id,company_id,search_term)
            VALUES(?,?,?) ON CONFLICT(term_id) DO UPDATE SET
            company_id=excluded.company_id,search_term=excluded.search_term""",
                   (term_id, company_id, term))
    db.commit()


def ensure_due_runs(db, now: datetime) -> None:
    cutoff = now - timedelta(days=7)
    for term_id, last_completed in db.execute(
            "SELECT term_id,last_completed_at FROM twenty_first_search_state ORDER BY term_id"):
        unfinished = db.execute("""SELECT 1 FROM twenty_first_search_jobs WHERE term_id=?
            AND status NOT IN ('done','manual_review') LIMIT 1""", (term_id,)).fetchone()
        if unfinished:
            continue
        if last_completed and datetime.fromisoformat(last_completed) > cutoff:
            continue
        mode = "backfill" if not last_completed else "incremental"
        run_id = f"{mode}:{term_id}:{now.date().isoformat()}"
        db.execute("""INSERT OR IGNORE INTO twenty_first_search_jobs
            (job_id,run_id,term_id,page,mode) VALUES(?,?,?,?,?)""",
                   (f"{run_id}:1", run_id, term_id, 1, mode))
    db.commit()


def plain(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(
        re.sub(r"<[^>]+>", "", str(value or "")))).strip()


def normalize_date(value: object, url: str = "") -> str | None:
    match = re.search(r"([12]\d{3})[-/.](\d{1,2})[-/.](\d{1,2})", str(value or ""))
    if not match:
        match = re.search(r"/article/([12]\d{3})(\d{2})(\d{2})/", url)
    if not match:
        return None
    try:
        return datetime(*(int(part) for part in match.groups())).date().isoformat()
    except ValueError:
        return None


def parse_jsonp(body: bytes, callback: str = CALLBACK) -> dict:
    pattern = rb"\s*" + re.escape(callback.encode()) + rb"\((.*)\)\s*;?\s*"
    match = re.fullmatch(pattern, body, re.DOTALL)
    if not match:
        raise ValueError("21财经 response is not the expected JSONP callback")
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise ValueError("21财经 JSONP payload is not an object")
    return payload


def decrypt_results(payload: dict, key: bytes) -> list[dict]:
    if payload.get("status") == -2:
        raise ValueError("21财经 search reported requests too frequent")
    if payload.get("status") != 1 or not isinstance(payload.get("list"), str):
        raise ValueError("21财经 search payload has invalid status/list")
    if len(key) not in (16, 24, 32):
        raise ValueError("21财经 search key has invalid AES length")
    try:
        encrypted = base64.b64decode(payload["list"], validate=True)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(IV)).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        decoded = unpadder.update(padded) + unpadder.finalize()
        result = json.loads(decoded)
    except Exception as exc:
        raise ValueError("21财经 encrypted result could not be decoded") from exc
    if not isinstance(result, list) or len(result) > PAGE_SIZE:
        raise ValueError("21财经 decoded result has invalid page size")
    return [item for item in result if isinstance(item, dict)]


def result_items(items: list[dict], exact_term: str) -> list[tuple[str, str, str, str]]:
    results: list[tuple[str, str, str, str]] = []
    for item in items:
        title = plain(item.get("title"))
        context = "\n".join((title, plain(item.get("description")), plain(item.get("highlight"))))
        if exact_term not in context:
            continue
        raw_url = urljoin("https://m.21jingji.com/", plain(item.get("url")))
        parsed = urlparse(raw_url)
        if parsed.hostname in {"m.21jingji.com", "www.21jingji.com"} and parsed.scheme == "http":
            raw_url = parsed._replace(scheme="https").geturl()
        candidate = canonicalize(raw_url)
        parsed = urlparse(candidate)
        if (parsed.scheme != "https" or parsed.hostname not in
                {"m.21jingji.com", "www.21jingji.com"} or
                not parsed.path.startswith("/article/")):
            continue
        published_at = normalize_date(item.get("inputtime"), candidate)
        if not published_at:
            continue
        document_type = "paywalled_news" if item.get("is_pay") else "independent_news"
        row = (candidate, title or candidate.rsplit("/", 1)[-1], published_at, document_type)
        if row not in results:
            results.append(row)
    return results


def key_url() -> str:
    return f"{KEY_ENDPOINT}?callback={CALLBACK}"


def search_url(term: str, page: int) -> str:
    return f"{SEARCH_ENDPOINT}?keywords={quote(term)}&page={page}&callback={CALLBACK}"


def store_hint(db, config: dict, company_id: str, url: str, title: str,
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
        (SOURCE_ID, canonicalize(url), title, published_at, company_id,
         config["entities"][company_id]["canonical_name"], document_type,
         "Asia/Shanghai", review_status))


def fetch_evidence(db, source, user_agent: str, url: str, title: str) -> bytes | None:
    if active_cooldown(db, SOURCE_ID):
        return None
    host = urlparse(url).hostname or ""
    reason = blocked_host(db, SOURCE_ID, host)
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
        block_host(db, SOURCE_ID, host, note)
        set_source_state(db, SOURCE_ID, 1, "manual_review", note)
        return None
    gate(url)
    started = time.monotonic()
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        error = f"{type(exc).__name__}: {str(exc)[:180]}"
        set_source_state(db, SOURCE_ID, 1, "cooldown", error, 300)
        audit({"source_id": SOURCE_ID, "outcome": "transport_error", "url": url,
               "route": source.route, "error": error, "cooldown_seconds": 300})
        return None
    elapsed = round((time.monotonic() - started) * 1000)
    event = {"source_id": SOURCE_ID, "url": url, "final_url": response.url,
             "http_status": response.status_code, "route": source.route,
             "elapsed_ms": elapsed}
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


def suspend_pending(db, reason: str) -> int:
    changed = 0
    for table in ("twenty_first_search_jobs", "twenty_first_detail_jobs"):
        changed += db.execute(
            f"UPDATE {table} SET status='manual_review',last_error=? WHERE status='pending'",
            (reason,)).rowcount
    db.execute("UPDATE twenty_first_search_state SET last_error=?", (reason,))
    db.commit()
    if changed:
        audit({"source_id": SOURCE_ID,
               "outcome": "twenty_first_search_suspended_manual_review",
               "affected_jobs": changed, "error": reason})
    return changed


def process_one(contact: str, now: datetime | None = None) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    reasons = [blocked_host(db, SOURCE_ID, host) for host in
               ("so.21jingji.com", "m.21jingji.com", "www.21jingji.com")]
    reason = next((value for value in reasons if value), None)
    if reason or (state and state[0] == "manual_review"):
        suspend_pending(db, reason or state[1] or "source requires manual review")
        db.close()
        return 0
    if active_cooldown(db, SOURCE_ID):
        db.close()
        return 0
    current = now or datetime.now(timezone.utc)
    ensure_due_runs(db, current)
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    job = db.execute("""SELECT j.job_id,j.run_id,j.term_id,j.page,j.mode,j.attempts,
        s.company_id,s.search_term FROM twenty_first_search_jobs j
        JOIN twenty_first_search_state s ON s.term_id=j.term_id
        WHERE j.status='pending' ORDER BY CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,
        j.term_id,j.page LIMIT 1""").fetchone()
    if job:
        job_id, run_id, term_id, page, mode, attempts, company_id, term = job
        query_url = search_url(term, page)
        store_hint(db, config, company_id, key_url(), "21财经公开检索密钥响应",
                   None, "search_response", "search_response")
        store_hint(db, config, company_id, query_url, f"21财经精确检索：{term} 第{page}页",
                   None, "search_response", "search_response")
        db.commit()
        try:
            key_body = fetch_evidence(db, source, user_agent, key_url(),
                                      "21财经公开检索密钥响应")
            if key_body is None:
                raise ValueError("21财经 search key response was not saved")
            key_payload = parse_jsonp(key_body)
            key = str(key_payload.get("key") or "").encode()
            body = fetch_evidence(db, source, user_agent, query_url,
                                  f"21财经精确检索：{term} 第{page}页")
            if body is None:
                raise ValueError("21财经 search response was not saved")
            payload = parse_jsonp(body)
            items = decrypt_results(payload, key)
            exact = result_items(items, term)
            observed = current.isoformat(timespec="seconds")
            skipped_old = 0
            exact_in_window = 0
            for candidate, title, published_at, document_type in exact:
                if published_at < str(source.raw["backfill_start"]):
                    skipped_old += 1
                    continue
                exact_in_window += 1
                store_hint(db, config, company_id, candidate, title, published_at, document_type)
                db.execute("""INSERT INTO twenty_first_detail_jobs
                    (url,term_id,company_id,title,published_at,document_type,mode,first_seen_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,published_at=excluded.published_at,
                    document_type=excluded.document_type""",
                    (candidate, term_id, company_id, title, published_at,
                     document_type, mode, observed))
            if len(items) == PAGE_SIZE and page < MAX_PAGES:
                next_page = page + 1
                db.execute("""INSERT OR IGNORE INTO twenty_first_search_jobs
                    (job_id,run_id,term_id,page,mode) VALUES(?,?,?,?,?)""",
                    (f"{run_id}:{next_page}", run_id, term_id, next_page, mode))
            db.execute("""UPDATE twenty_first_search_jobs SET status='done',attempts=?,
                skipped_before_window=?,last_error=NULL,processed_at=? WHERE job_id=?""",
                       (attempts + 1, skipped_old, observed, job_id))
            remaining = db.execute("""SELECT 1 FROM twenty_first_search_jobs
                WHERE run_id=? AND status!='done' LIMIT 1""", (run_id,)).fetchone()
            if not remaining:
                pages = db.execute("SELECT count(*) FROM twenty_first_search_jobs WHERE run_id=?",
                                   (run_id,)).fetchone()[0]
                skipped_total = db.execute("""SELECT COALESCE(SUM(skipped_before_window),0)
                    FROM twenty_first_search_jobs WHERE run_id=?""", (run_id,)).fetchone()[0]
                exact_count = db.execute(
                    "SELECT count(*) FROM twenty_first_detail_jobs WHERE term_id=?", (term_id,)).fetchone()[0]
                db.execute("""UPDATE twenty_first_search_state SET last_completed_at=?,
                    pages_scanned=?,exact_results=?,skipped_before_window=?,last_error=NULL
                    WHERE term_id=?""",
                    (observed, pages, exact_count, skipped_total, term_id))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "twenty_first_search_success",
                   "job_id": job_id, "term_id": term_id, "page": page,
                   "page_results": len(items), "exact_in_window": exact_in_window,
                   "skipped_before_window": skipped_old})
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            if "too frequent" in str(exc):
                set_source_state(db, SOURCE_ID, 1, "cooldown", str(exc), 3600)
            source_state = db.execute("SELECT state FROM source_state WHERE source_id=?",
                                      (SOURCE_ID,)).fetchone()
            status = "manual_review" if (source_state and source_state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
            db.execute("""UPDATE twenty_first_search_jobs SET status=?,attempts=?,last_error=?
                WHERE job_id=?""", (status, attempts + 1, str(exc)[:300], job_id))
            db.execute("UPDATE twenty_first_search_state SET last_error=? WHERE term_id=?",
                       (str(exc)[:300], term_id))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "twenty_first_search_error",
                   "job_id": job_id, "error": str(exc)[:300]})
        db.close()
        return 1

    detail = db.execute("""SELECT url,published_at,document_type,attempts
        FROM twenty_first_detail_jobs WHERE status='pending'
        ORDER BY published_at DESC,url LIMIT 1""").fetchone()
    if not detail:
        db.close()
        return 0
    url, published_at, document_type, attempts = detail
    collect_source(db, source, user_agent, 1, False, url)
    saved = db.execute("SELECT status,raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                       (SOURCE_ID, canonicalize(url))).fetchone()
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    if saved and saved[0] == 200 and saved[1] and source_state and source_state[0] == "closed":
        processed = current.isoformat(timespec="seconds")
        db.execute("""UPDATE twenty_first_detail_jobs SET status='done',attempts=?,
            last_error=NULL,processed_at=? WHERE url=?""", (attempts + 1, processed, url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "twenty_first_detail_success",
               "url": url, "published_at": published_at, "document_type": document_type})
    else:
        error = (source_state[1] if source_state and source_state[1] else
                 "21财经 detail response was not saved")
        status = "manual_review" if (source_state and source_state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("""UPDATE twenty_first_detail_jobs SET status=?,attempts=?,last_error=?
            WHERE url=?""", (status, attempts + 1, error[:300], url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "twenty_first_detail_error",
               "url": url, "error": error[:300]})
    db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    print(f"processed={process_one(contact)}")
