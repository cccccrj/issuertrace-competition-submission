#!/usr/bin/env python3
"""Low-volume exact-entity discovery through Jiemian's public HTML search."""
from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from collector.collect import (
    ROOT, active_cooldown, audit, blocked_host, canonicalize, collect_source,
    connect, load_sources,
)

SOURCE_ID = "jiemian"
SEARCH_HOST = "a.jiemian.com"
DETAIL_HOST = "www.jiemian.com"
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
    published_at: str


def plain(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", value))).strip()


def search_url(term: str, page: int) -> str:
    return ("https://a.jiemian.com/index.php?m=search&a=index&"
            f"msg={quote(term)}&opt=old&page={page}")


def parse_date(value: str) -> str | None:
    match = re.search(r"([12]\d{3})/(\d{1,2})/(\d{1,2})", value)
    if not match:
        return None
    try:
        return datetime(*(int(part) for part in match.groups())).date().isoformat()
    except ValueError:
        return None


def parse_results(body: bytes) -> list[SearchItem]:
    source = body.decode("utf-8", errors="ignore")
    starts = [match.start() for match in re.finditer(
        r'<div\s+class=["\']news-view\s+left["\']', source, re.I)]
    if len(starts) > 15:
        raise ValueError("Jiemian search page exceeds 15-result guard")
    starts.append(source.find("<!--分页-->", starts[-1] if starts else 0))
    if starts[-1] < 0:
        starts[-1] = len(source)
    results: list[SearchItem] = []
    seen: set[str] = set()
    for index in range(len(starts) - 1):
        block = source[starts[index]:starts[index + 1]]
        link = re.search(
            r'href=["\'](https://www\.jiemian\.com/article/\d+\.html)["\']',
            block, re.I)
        title_match = re.search(
            r'<h3[^>]*>\s*<a[^>]+title=["\']([^"\']+)["\']', block, re.I | re.S)
        date_match = re.search(
            r'<span\s+class=["\']date["\'][^>]*>(.*?)</span>', block, re.I | re.S)
        if not link or not title_match or not date_match:
            continue
        url = canonicalize(link.group(1))
        parsed = urlparse(url)
        published_at = parse_date(plain(date_match.group(1)))
        if (parsed.scheme != "https" or parsed.hostname != DETAIL_HOST or
                not re.fullmatch(r"/article/\d+\.html", parsed.path) or
                not published_at or url in seen):
            continue
        summary_match = re.search(
            r'<div\s+class=["\']news-main["\'][^>]*>\s*<p[^>]*>(.*?)</p>',
            block, re.I | re.S)
        results.append(SearchItem(
            url=url,
            title=plain(title_match.group(1)),
            summary=plain(summary_match.group(1)) if summary_match else "",
            published_at=published_at,
        ))
        seen.add(url)
    return results


def exact_results(items: list[SearchItem], term: str) -> list[SearchItem]:
    return [item for item in items if term in f"{item.title} {item.summary}"]


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS jiemian_search_state (
        term_id TEXT PRIMARY KEY,company_id TEXT NOT NULL,search_term TEXT NOT NULL,
        last_completed_at TEXT,pages_scanned INTEGER NOT NULL DEFAULT 0,
        exact_results INTEGER NOT NULL DEFAULT 0,
        skipped_before_window INTEGER NOT NULL DEFAULT 0,
        coverage_note TEXT NOT NULL DEFAULT 'not_started',last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS jiemian_search_jobs (
        job_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,term_id TEXT NOT NULL,
        page INTEGER NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,skipped_before_window INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,processed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS jiemian_detail_jobs (
        url TEXT PRIMARY KEY,term_id TEXT NOT NULL,company_id TEXT NOT NULL,
        title TEXT NOT NULL,summary TEXT,published_at TEXT NOT NULL,
        document_type TEXT NOT NULL DEFAULT 'independent_news',mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,processed_at TEXT,first_seen_at TEXT NOT NULL)""")
    for term_id, (company_id, term) in TERMS.items():
        db.execute("""INSERT INTO jiemian_search_state(term_id,company_id,search_term)
            VALUES(?,?,?) ON CONFLICT(term_id) DO UPDATE SET
            company_id=excluded.company_id,search_term=excluded.search_term""",
                   (term_id, company_id, term))
    db.commit()


def ensure_due_runs(db: sqlite3.Connection, now: datetime) -> None:
    cutoff = now - timedelta(days=1)
    for term_id, last_completed in db.execute(
            "SELECT term_id,last_completed_at FROM jiemian_search_state ORDER BY term_id"):
        unfinished = db.execute("""SELECT 1 FROM jiemian_search_jobs WHERE term_id=?
            AND status NOT IN ('done','manual_review') LIMIT 1""", (term_id,)).fetchone()
        if unfinished or (last_completed and datetime.fromisoformat(last_completed) > cutoff):
            continue
        mode = "backfill" if not last_completed else "incremental"
        run_id = f"{mode}:{term_id}:{now.date().isoformat()}"
        db.execute("""INSERT OR IGNORE INTO jiemian_search_jobs
            (job_id,run_id,term_id,page,mode) VALUES(?,?,?,?,?)""",
                   (f"{run_id}:1", run_id, term_id, 1, mode))
        db.execute("UPDATE jiemian_search_state SET coverage_note='in_progress' WHERE term_id=?",
                   (term_id,))
    db.commit()


def store_hint(db: sqlite3.Connection, config: dict, company_id: str, url: str,
               title: str, published_at: str | None, document_type: str,
               review_status: str) -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,published_at=COALESCE(excluded.published_at,discovered_metadata.published_at),
        company_id=excluded.company_id,matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, published_at,
         None if review_status == "search_response" else company_id,
         None if review_status == "search_response" else config["entities"][company_id]["canonical_name"],
         document_type, "Asia/Shanghai", review_status))


def saved_raw(db: sqlite3.Connection, url: str) -> tuple[str, int] | None:
    row = db.execute("""SELECT raw_path,status FROM documents
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url))).fetchone()
    return (row[0], row[1]) if row and row[0] else None


def suspend_pending(db: sqlite3.Connection, reason: str) -> None:
    changed = 0
    for table in ("jiemian_search_jobs", "jiemian_detail_jobs"):
        changed += db.execute(
            f"UPDATE {table} SET status='manual_review',last_error=? WHERE status='pending'",
            (reason,)).rowcount
    db.execute("UPDATE jiemian_search_state SET last_error=?", (reason,))
    db.commit()
    if changed:
        audit({"source_id": SOURCE_ID, "outcome": "jiemian_search_suspended_manual_review",
               "affected_jobs": changed, "error": reason})


def focus_company(config: dict) -> str | None:
    focus = (config.get("collection_defaults", {}).get("scheduling", {})
             .get("focus_company"))
    return focus if focus in TERMS else None


def next_detail_job(db: sqlite3.Connection, focus: str | None = None):
    """Pick the least-served entity before recency among queued detail pages."""
    return db.execute("""SELECT j.url,j.term_id,j.company_id,j.title,j.published_at,
        j.document_type,j.attempts FROM jiemian_detail_jobs j
        LEFT JOIN (SELECT term_id,count(*) AS completed FROM jiemian_detail_jobs
                   WHERE status='done' GROUP BY term_id) progress ON progress.term_id=j.term_id
        WHERE j.status='pending'
        ORDER BY CASE WHEN j.term_id=? THEN 0 ELSE 1 END,
                 COALESCE(progress.completed,0),j.published_at DESC,j.url LIMIT 1""",
        (focus or "",)).fetchone()


def finish_detail(db: sqlite3.Connection, source, config: dict, contact: str,
                  current: datetime) -> bool:
    job = next_detail_job(db, focus_company(config))
    if not job:
        return False
    url, term_id, company_id, title, published_at, document_type, attempts = job
    store_hint(db, config, company_id, url, title, published_at,
               document_type, "candidate_entity_match")
    db.commit()
    collect_source(db, source, f"ListedCompanyEventIntelligence/0.1 (+{contact})", 1, False, url)
    saved = saved_raw(db, url)
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    if saved and saved[1] == 200 and state and state[0] == "closed":
        db.execute("""UPDATE jiemian_detail_jobs SET status='done',attempts=?,
            last_error=NULL,processed_at=? WHERE url=?""",
                   (attempts + 1, current.isoformat(timespec="seconds"), url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "jiemian_detail_success",
               "url": url, "published_at": published_at})
    else:
        error = state[1] if state and state[1] else "Jiemian detail response was not saved"
        status = "manual_review" if (state and state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("""UPDATE jiemian_detail_jobs SET status=?,attempts=?,last_error=?
            WHERE url=?""", (status, attempts + 1, error[:300], url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "jiemian_detail_error",
               "url": url, "error": error[:300]})
    return True


def next_search_job(db: sqlite3.Connection, focus: str | None = None):
    return db.execute("""SELECT j.job_id,j.run_id,j.term_id,j.page,j.mode,j.attempts,
        s.company_id,s.search_term FROM jiemian_search_jobs j JOIN jiemian_search_state s
        ON s.term_id=j.term_id WHERE j.status='pending'
        ORDER BY CASE WHEN j.term_id=? THEN 0 ELSE 1 END,
                 CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,
                 s.pages_scanned,j.term_id,j.page LIMIT 1""", (focus or "",)).fetchone()


def should_prioritize_search(db: sqlite3.Connection, focus: str | None = None) -> bool:
    """Alternate result-page and detail work while a search job remains.

    This keeps a large detail queue from blocking the less-scanned entity's
    search frontier.  It merely selects the next existing job and does not
    bypass cooldowns, robots checks, or the configured cadence.
    """
    if not next_search_job(db, focus):
        return False
    last_search = db.execute("""SELECT MAX(processed_at) FROM jiemian_search_jobs
        WHERE status='done'""").fetchone()[0]
    last_detail = db.execute("""SELECT MAX(processed_at) FROM jiemian_detail_jobs
        WHERE status='done'""").fetchone()[0]
    return not last_search or (last_detail is not None and last_detail >= last_search)


def finish_search(db: sqlite3.Connection, source, config: dict, contact: str,
                  current: datetime) -> bool:
    job = next_search_job(db, focus_company(config))
    if not job:
        return False
    job_id, run_id, term_id, page, mode, attempts, company_id, term = job
    url = search_url(term, page)
    store_hint(db, config, company_id, url, f"界面新闻精确检索：{term} 第{page}页",
               None, "search_response", "search_response")
    db.commit()
    try:
        collect_source(db, source, f"ListedCompanyEventIntelligence/0.1 (+{contact})",
                       1, False, url)
        saved = saved_raw(db, url)
        state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                           (SOURCE_ID,)).fetchone()
        if not saved or saved[1] != 200 or not state or state[0] != "closed":
            raise ValueError(state[1] if state and state[1]
                             else "Jiemian search response was not saved")
        items = parse_results((ROOT / saved[0]).read_bytes())
        exact = exact_results(items, term)
        observed = current.isoformat(timespec="seconds")
        known = {row[0] for row in db.execute(
            "SELECT url FROM jiemian_detail_jobs WHERE term_id=?", (term_id,))}
        known_boundary = mode == "incremental" and any(item.url in known for item in exact)
        skipped_old = 0
        exact_in_window = 0
        for item in exact:
            if item.published_at < str(source.raw["backfill_start"]):
                skipped_old += 1
                continue
            exact_in_window += 1
            store_hint(db, config, company_id, item.url, item.title,
                       item.published_at, "independent_news", "candidate_entity_match")
            db.execute("""INSERT INTO jiemian_detail_jobs
                (url,term_id,company_id,title,summary,published_at,mode,first_seen_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,summary=excluded.summary,published_at=excluded.published_at""",
                (item.url, term_id, company_id, item.title, item.summary,
                 item.published_at, mode, observed))
        date_boundary = bool(items and min(item.published_at for item in items) <
                             str(source.raw["backfill_start"]))
        guard_boundary = page >= MAX_PAGES
        continue_pages = (len(items) == PAGE_SIZE and not date_boundary and
                          not known_boundary and not guard_boundary)
        if continue_pages:
            next_page = page + 1
            db.execute("""INSERT OR IGNORE INTO jiemian_search_jobs
                (job_id,run_id,term_id,page,mode) VALUES(?,?,?,?,?)""",
                (f"{run_id}:{next_page}", run_id, term_id, next_page, mode))
        db.execute("""UPDATE jiemian_search_jobs SET status='done',attempts=?,
            skipped_before_window=?,last_error=NULL,processed_at=? WHERE job_id=?""",
                   (attempts + 1, skipped_old, observed, job_id))
        pages = db.execute("""SELECT count(*) FROM jiemian_search_jobs
            WHERE run_id=? AND status='done'""", (run_id,)).fetchone()[0]
        skipped = db.execute("""SELECT COALESCE(SUM(skipped_before_window),0)
            FROM jiemian_search_jobs WHERE run_id=?""", (run_id,)).fetchone()[0]
        exact_count = db.execute(
            "SELECT count(*) FROM jiemian_detail_jobs WHERE term_id=?", (term_id,)).fetchone()[0]
        db.execute("""UPDATE jiemian_search_state SET pages_scanned=?,exact_results=?,
            skipped_before_window=?,last_error=NULL WHERE term_id=?""",
                   (pages, exact_count, skipped, term_id))
        if not continue_pages:
            coverage = ("incremental_known_boundary" if known_boundary else
                        "date_boundary_reached" if date_boundary else
                        "max_pages_guard_reached" if guard_boundary else
                        "result_exhausted")
            db.execute("""UPDATE jiemian_search_state SET last_completed_at=?,pages_scanned=?,
                exact_results=?,skipped_before_window=?,coverage_note=?,last_error=NULL
                WHERE term_id=?""", (observed, pages, exact_count, skipped, coverage, term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "jiemian_search_success",
               "job_id": job_id, "term_id": term_id, "page": page,
               "page_results": len(items), "exact_in_window": exact_in_window,
               "skipped_before_window": skipped_old, "continued": continue_pages})
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        state = db.execute("SELECT state FROM source_state WHERE source_id=?",
                           (SOURCE_ID,)).fetchone()
        status = "manual_review" if (state and state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("UPDATE jiemian_search_jobs SET status=?,attempts=?,last_error=? WHERE job_id=?",
                   (status, attempts + 1, str(exc)[:300], job_id))
        db.execute("UPDATE jiemian_search_state SET last_error=? WHERE term_id=?",
                   (str(exc)[:300], term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "jiemian_search_error",
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
    reason = next((blocked_host(db, SOURCE_ID, host)
                   for host in (SEARCH_HOST, DETAIL_HOST)
                   if blocked_host(db, SOURCE_ID, host)), None)
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
    if should_prioritize_search(db, focus):
        processed = (finish_search(db, source, config, contact, current) or
                     finish_detail(db, source, config, contact, current))
    else:
        processed = (finish_detail(db, source, config, contact, current) or
                     finish_search(db, source, config, contact, current))
    db.close()
    return int(processed)


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    print(f"processed={process_one(contact)}")
