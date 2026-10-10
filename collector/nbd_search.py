#!/usr/bin/env python3
"""Exact legal-name search through NBD's public same-origin JSON endpoint."""
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

SOURCE_ID = "nbd"
ENDPOINT = "https://www.nbd.com.cn/news-search/queryByMatch"
PAGE_SIZE = 25
TERMS = {
    "cxmt_legal": ("cxmt", "长鑫科技集团股份有限公司"),
    "wuxi_legal": ("wuxi_apptec", "无锡药明康德新药开发股份有限公司"),
}


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS nbd_search_state (
        term_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, search_term TEXT NOT NULL,
        last_completed_at TEXT, total_results INTEGER, exact_results INTEGER,
        last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS nbd_search_jobs (
        job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, term_id TEXT NOT NULL,
        result_offset INTEGER NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS nbd_detail_jobs (
        url TEXT PRIMARY KEY, term_id TEXT NOT NULL, company_id TEXT NOT NULL,
        title TEXT NOT NULL, published_at TEXT NOT NULL, document_type TEXT NOT NULL,
        mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, processed_at TEXT,
        first_seen_at TEXT NOT NULL)""")
    for term_id, (company_id, term) in TERMS.items():
        db.execute("""INSERT INTO nbd_search_state(term_id,company_id,search_term)
            VALUES(?,?,?) ON CONFLICT(term_id) DO UPDATE SET
            company_id=excluded.company_id,search_term=excluded.search_term""",
                   (term_id, company_id, term))
    db.commit()


def ensure_due_runs(db, now: datetime) -> None:
    cutoff = now - timedelta(days=7)
    for term_id, last_completed in db.execute("""SELECT term_id,last_completed_at
            FROM nbd_search_state ORDER BY term_id""").fetchall():
        unfinished = db.execute("""SELECT 1 FROM nbd_search_jobs WHERE term_id=?
            AND status NOT IN ('done','manual_review') LIMIT 1""", (term_id,)).fetchone()
        if unfinished:
            continue
        if last_completed and datetime.fromisoformat(last_completed) > cutoff:
            continue
        mode = "backfill" if not last_completed else "incremental"
        run_id = f"{mode}:{term_id}:{now.date().isoformat()}"
        db.execute("""INSERT OR IGNORE INTO nbd_search_jobs
            (job_id,run_id,term_id,result_offset,mode) VALUES(?,?,?,?,?)""",
                   (f"{run_id}:0", run_id, term_id, 0, mode))
    db.commit()


def plain(value: object) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()


def normalize_date(value: object) -> str | None:
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()
    match = re.search(r"([12]\d{3})[-/.](\d{1,2})[-/.](\d{1,2})", str(value or ""))
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date().isoformat()
    except ValueError:
        return None


def result_items(payload: object, exact_term: str) -> tuple[int, list[tuple[str, str, str, str]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("NBD search response has no data object")
    data = payload["data"]
    total = data.get("totalHits")
    results = data.get("searchResults")
    if not isinstance(total, int) or not isinstance(results, list):
        raise ValueError("NBD search response has invalid total/results")
    parsed_items: list[tuple[str, str, str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = plain(item.get("titleText") or item.get("title"))
        context = "\n".join((title, plain(item.get("digestText") or item.get("digest")),
                              plain(item.get("contentText") or item.get("content"))))
        if exact_term not in context:
            continue
        candidate = urljoin("https://www.nbd.com.cn/", str(item.get("url") or ""))
        parsed_candidate = urlparse(candidate)
        if parsed_candidate.hostname == "www.nbd.com.cn" and parsed_candidate.scheme == "http":
            candidate = parsed_candidate._replace(scheme="https").geturl()
        candidate = canonicalize(candidate)
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or parsed.hostname != "www.nbd.com.cn" or "/articles/" not in parsed.path:
            continue
        published_at = normalize_date(item.get("publishTime"))
        if not published_at:
            continue
        document_type = "news_brief" if item.get("flashType") == 2 else "independent_news"
        row = (candidate, title or candidate.rsplit("/", 1)[-1], published_at, document_type)
        if row not in parsed_items:
            parsed_items.append(row)
    return total, parsed_items


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


def evidence_url(term: str, result_offset: int) -> str:
    return ENDPOINT + "?" + urlencode({
        "keyword": term, "from": result_offset, "size": PAGE_SIZE,
        "platform": "0,1", "includeAd": "false",
    })


def fetch_search_response(db, source, user_agent: str, url: str, term: str,
                          result_offset: int) -> bytes | None:
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
        response = session.post(url, json={
            "keyword": term, "from": result_offset, "size": PAGE_SIZE,
            "includeAd": False, "platform": [0, 1],
        }, timeout=timeout, allow_redirects=True)
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
    record_document(db, source, url, response, digest, raw_path,
                    f"NBD exact search: {term}", "search_response")
    set_source_state(db, SOURCE_ID, 0, "closed", None)
    audit({**event, "outcome": "success", "content_hash": digest,
           "raw_path": raw_path, "bytes": len(response.content),
           "title": f"NBD exact search: {term}"})
    return response.content


def suspend_pending(db, reason: str) -> int:
    changed = 0
    for table in ("nbd_search_jobs", "nbd_detail_jobs"):
        changed += db.execute(
            f"UPDATE {table} SET status='manual_review',last_error=? WHERE status='pending'",
            (reason,)).rowcount
    db.execute("UPDATE nbd_search_state SET last_error=?", (reason,))
    db.commit()
    if changed:
        audit({"source_id": SOURCE_ID,
               "outcome": "nbd_search_suspended_manual_review",
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
    reason = blocked_host(db, SOURCE_ID, "www.nbd.com.cn")
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
    search_job = db.execute("""SELECT j.job_id,j.run_id,j.term_id,j.result_offset,j.mode,j.attempts,
        s.company_id,s.search_term FROM nbd_search_jobs j JOIN nbd_search_state s
        ON s.term_id=j.term_id WHERE j.status='pending'
        ORDER BY CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,j.term_id,j.result_offset LIMIT 1""").fetchone()
    if search_job:
        job_id, run_id, term_id, result_offset, mode, attempts, company_id, term = search_job
        url = evidence_url(term, result_offset)
        store_hint(db, config, company_id, url, f"NBD exact search: {term}", None,
                   "search_response", "search_response")
        db.commit()
        body = fetch_search_response(db, source, user_agent, url, term, result_offset)
        try:
            if body is None:
                raise ValueError("NBD search response was not saved")
            payload = json.loads(body.decode("utf-8"))
            total, results = result_items(payload, term)
            if total > 500:
                raise ValueError(f"exact legal-name search returned implausible total {total}")
            if result_offset == 0:
                for offset in range(PAGE_SIZE, total, PAGE_SIZE):
                    db.execute("""INSERT OR IGNORE INTO nbd_search_jobs
                        (job_id,run_id,term_id,result_offset,mode) VALUES(?,?,?,?,?)""",
                        (f"{run_id}:{offset}", run_id, term_id, offset, mode))
            exact_in_window = 0
            skipped_old = 0
            observed = current.isoformat(timespec="seconds")
            for candidate, title, published_at, document_type in results:
                if published_at < str(source.raw["backfill_start"]):
                    skipped_old += 1
                    continue
                exact_in_window += 1
                store_hint(db, config, company_id, candidate, title, published_at, document_type)
                db.execute("""INSERT INTO nbd_detail_jobs
                    (url,term_id,company_id,title,published_at,document_type,mode,first_seen_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,published_at=excluded.published_at,
                    document_type=excluded.document_type""",
                    (candidate, term_id, company_id, title, published_at,
                     document_type, mode, observed))
            db.execute("""UPDATE nbd_search_jobs SET status='done',attempts=?,last_error=NULL,
                processed_at=? WHERE job_id=?""", (attempts + 1, observed, job_id))
            remaining = db.execute("""SELECT 1 FROM nbd_search_jobs WHERE run_id=?
                AND status!='done' LIMIT 1""", (run_id,)).fetchone()
            if not remaining:
                exact_count = db.execute("SELECT count(*) FROM nbd_detail_jobs WHERE term_id=?",
                                         (term_id,)).fetchone()[0]
                db.execute("""UPDATE nbd_search_state SET last_completed_at=?,total_results=?,
                    exact_results=?,last_error=NULL WHERE term_id=?""",
                    (observed, total, exact_count, term_id))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "nbd_search_success",
                   "job_id": job_id, "term_id": term_id, "offset": result_offset,
                   "total_results": total, "exact_in_window": exact_in_window,
                   "skipped_before_window": skipped_old})
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            source_state = db.execute("SELECT state FROM source_state WHERE source_id=?",
                                      (SOURCE_ID,)).fetchone()
            status = "manual_review" if (source_state and source_state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
            db.execute("""UPDATE nbd_search_jobs SET status=?,attempts=?,last_error=?
                WHERE job_id=?""", (status, attempts + 1, str(exc)[:300], job_id))
            db.execute("UPDATE nbd_search_state SET last_error=? WHERE term_id=?",
                       (str(exc)[:300], term_id))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "nbd_search_error",
                   "job_id": job_id, "error": str(exc)[:300]})
        db.close()
        return 1

    detail = db.execute("""SELECT url,title,published_at,document_type,attempts FROM nbd_detail_jobs
        WHERE status='pending' ORDER BY published_at DESC,url LIMIT 1""").fetchone()
    if not detail:
        db.close()
        return 0
    url, title, published_at, document_type, attempts = detail
    collect_source(db, source, user_agent, 1, False, url)
    saved = db.execute("SELECT status,raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                       (SOURCE_ID, canonicalize(url))).fetchone()
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    if saved and saved[0] == 200 and saved[1] and source_state and source_state[0] == "closed":
        processed = current.isoformat(timespec="seconds")
        db.execute("""UPDATE nbd_detail_jobs SET status='done',attempts=?,last_error=NULL,
            processed_at=? WHERE url=?""", (attempts + 1, processed, url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "nbd_detail_success", "url": url,
               "published_at": published_at, "document_type": document_type})
    else:
        reason = (source_state[1] if source_state and source_state[1] else
                  "NBD detail response was not saved")
        status = "manual_review" if (source_state and source_state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("""UPDATE nbd_detail_jobs SET status=?,attempts=?,last_error=? WHERE url=?""",
                   (status, attempts + 1, reason[:300], url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "nbd_detail_error",
               "url": url, "error": reason[:300]})
    db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    print(f"processed={process_one(contact)}")
