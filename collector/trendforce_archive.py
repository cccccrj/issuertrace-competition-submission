#!/usr/bin/env python3
"""Low-volume CXMT discovery from TrendForce's public semiconductor archive."""
from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from collector.collect import (
    ROOT, active_cooldown, audit, blocked_host, canonicalize, collect_source,
    connect, load_sources,
)

SOURCE_ID = "trendforce"
ARCHIVE_URL = "https://www.trendforce.com/presscenter/news/Semiconductors"
EXPECTED_HOST = "www.trendforce.com"
PAGE_SIZE = 5
MAX_PAGES = 100
ENTITY_RE = re.compile(r"(?<![A-Za-z0-9])CXMT(?![A-Za-z0-9])|ChangXin Memory Technologies", re.I)
DETAIL_PATH_RE = re.compile(r"^/presscenter/news/([12]\d{3})(\d{2})(\d{2})-\d+\.html$")


@dataclass(frozen=True)
class ArchiveItem:
    url: str
    title: str
    published_at: str
    document_type: str = "market_research"


class _ArchiveParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.card_depth = 0
        self.card_text: list[str] = []
        self.anchor_depth = 0
        self.anchor_href: str | None = None
        self.anchor_text: list[str] = []
        self.cards: list[tuple[str | None, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "div" and not self.card_depth and "list-item" in classes:
            self.card_depth = 1
            self.card_text = []
            return
        if not self.card_depth:
            return
        if tag == "div":
            self.card_depth += 1
        if self.anchor_depth:
            self.anchor_depth += 1
        elif tag == "a" and "title-link" in classes:
            self.anchor_depth = 1
            self.anchor_href = values.get("href")
            self.anchor_text = []

    def handle_data(self, data: str) -> None:
        if self.card_depth:
            self.card_text.append(data)
        if self.anchor_depth:
            self.anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.card_depth:
            return
        if self.anchor_depth:
            self.anchor_depth -= 1
            if not self.anchor_depth:
                title = re.sub(r"\s+", " ", html.unescape("".join(self.anchor_text))).strip()
                self.cards.append((self.anchor_href, title, ""))
                self.anchor_href = None
                self.anchor_text = []
            return
        if tag == "div":
            self.card_depth -= 1
            if not self.card_depth and self.cards:
                href, title, _ = self.cards[-1]
                text = re.sub(r"\s+", " ", html.unescape("".join(self.card_text))).strip()
                self.cards[-1] = (href, title, text)
                self.card_text = []


def parse_archive_page(body: bytes, base_url: str = ARCHIVE_URL) -> tuple[list[str], list[ArchiveItem]]:
    parser = _ArchiveParser(base_url)
    parser.feed(body.decode("utf-8", errors="ignore"))
    dates: list[str] = []
    items: list[ArchiveItem] = []
    seen: set[str] = set()
    for href, title, text in parser.cards:
        if not href or not title:
            continue
        url = canonicalize(urljoin(base_url, href))
        parsed = urlparse(url)
        match = DETAIL_PATH_RE.fullmatch(parsed.path)
        if (parsed.scheme != "https" or parsed.hostname != EXPECTED_HOST or
                parsed.query or not match or url in seen):
            continue
        published = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        try:
            datetime.strptime(published, "%Y-%m-%d")
        except ValueError:
            continue
        dates.append(published)
        if ENTITY_RE.search(title + " " + text):
            items.append(ArchiveItem(url, title, published))
        seen.add(url)
    return dates, items


def page_url(page: int) -> str:
    if page < 1 or page > MAX_PAGES:
        raise ValueError("TrendForce archive page outside guard")
    return ARCHIVE_URL if page == 1 else f"{ARCHIVE_URL}?page={page}"


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS trendforce_archive_state (
        source_id TEXT PRIMARY KEY, last_completed_at TEXT,
        pages_scanned INTEGER NOT NULL DEFAULT 0,
        exact_results INTEGER NOT NULL DEFAULT 0,
        skipped_before_window INTEGER NOT NULL DEFAULT 0,
        coverage_note TEXT NOT NULL DEFAULT 'semiconductor_archive_title_summary',
        last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS trendforce_archive_jobs (
        job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, page INTEGER NOT NULL,
        mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, exact_results INTEGER NOT NULL DEFAULT 0,
        skipped_before_window INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT, UNIQUE(run_id,page))""")
    db.execute("""CREATE TABLE IF NOT EXISTS trendforce_detail_jobs (
        url TEXT PRIMARY KEY, title TEXT NOT NULL, published_at TEXT NOT NULL,
        document_type TEXT NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT, first_seen_at TEXT NOT NULL)""")
    db.execute("""INSERT OR IGNORE INTO trendforce_archive_state(source_id)
        VALUES(?)""", (SOURCE_ID,))
    db.commit()


def ensure_due_run(db: sqlite3.Connection, now: datetime | None = None) -> int:
    if db.execute("SELECT 1 FROM trendforce_archive_jobs WHERE status='pending' LIMIT 1").fetchone():
        return 0
    current = now or datetime.now(timezone.utc)
    row = db.execute("SELECT last_completed_at FROM trendforce_archive_state WHERE source_id=?",
                     (SOURCE_ID,)).fetchone()
    if row and row[0] and datetime.fromisoformat(row[0]) + timedelta(days=7) > current:
        return 0
    mode = "incremental" if row and row[0] else "backfill"
    run_id = f"{mode}:{SOURCE_ID}:{current.date().isoformat()}"
    job_id = f"{run_id}:1"
    inserted = db.execute("""INSERT OR IGNORE INTO trendforce_archive_jobs
        (job_id,run_id,page,mode) VALUES(?,?,1,?)""", (job_id, run_id, mode)).rowcount
    db.commit()
    return inserted


def store_search_hint(db: sqlite3.Connection, url: str, page: int) -> None:
    title = f"TrendForce semiconductor archive page {page}"
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,document_type,review_status)
        VALUES(?,?,?,'search_response','search_response')
        ON CONFLICT(source_id,canonical_url) DO UPDATE SET title=excluded.title,
        document_type='search_response',review_status='search_response'""",
        (SOURCE_ID, canonicalize(url), title))
    db.execute("""UPDATE documents SET review_status='search_response'
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url)))
    db.execute("""UPDATE document_versions SET document_type='search_response',
        review_status='search_response',company_id=NULL,matched_legal_entity=NULL
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url)))


def store_candidate(db: sqlite3.Connection, source, item: ArchiveItem, mode: str,
                    observed: str) -> None:
    legal_name = source.raw.get("_canonical_names", {}).get("cxmt")
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,'cxmt',?,?,?,'candidate_entity_match')
        ON CONFLICT(source_id,canonical_url) DO UPDATE SET title=excluded.title,
        published_at=excluded.published_at,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, item.url, item.title, item.published_at, legal_name,
         item.document_type, "Asia/Taipei"))
    db.execute("""INSERT INTO trendforce_detail_jobs
        (url,title,published_at,document_type,mode,first_seen_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
        published_at=excluded.published_at,document_type=excluded.document_type""",
        (item.url, item.title, item.published_at, item.document_type, mode, observed))


def _saved(db: sqlite3.Connection, url: str):
    return db.execute("""SELECT status,raw_path FROM documents
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url))).fetchone()


def suspend_pending(db: sqlite3.Connection, reason: str) -> None:
    changed = 0
    for table in ("trendforce_archive_jobs", "trendforce_detail_jobs"):
        changed += db.execute(
            f"UPDATE {table} SET status='manual_review',last_error=? WHERE status='pending'",
            (reason,)).rowcount
    db.execute("UPDATE trendforce_archive_state SET last_error=? WHERE source_id=?",
               (reason, SOURCE_ID))
    db.commit()
    if changed:
        audit({"source_id": SOURCE_ID,
               "outcome": "trendforce_archive_suspended_manual_review",
               "affected_jobs": changed, "error": reason})


def process_one(contact: str, now: datetime | None = None) -> int:
    _, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    current = now or datetime.now(timezone.utc)
    ensure_due_run(db, current)
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    reason = blocked_host(db, SOURCE_ID, EXPECTED_HOST)
    if reason or (source_state and source_state[0] == "manual_review"):
        suspend_pending(db, reason or source_state[1] or "source requires manual review")
        db.close()
        return 0

    detail = db.execute("""SELECT url,title,published_at,document_type,mode,attempts
        FROM trendforce_detail_jobs WHERE status='pending'
        ORDER BY published_at DESC,url LIMIT 1""").fetchone()
    if detail:
        url, title, published, kind, mode, attempts = detail
        item = ArchiveItem(url, title, published, kind)
        store_candidate(db, source, item, mode, current.isoformat(timespec="seconds"))
        db.commit()
        collect_source(db, source, user_agent, 1, False, url)
        saved = _saved(db, url)
        source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                                  (SOURCE_ID,)).fetchone()
        if saved and int(saved[0]) == 200 and saved[1] and source_state and source_state[0] == "closed":
            processed = current.isoformat(timespec="seconds")
            db.execute("""UPDATE trendforce_detail_jobs SET status='done',attempts=?,
                last_error=NULL,processed_at=? WHERE url=?""",
                (attempts + 1, processed, url))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "trendforce_detail_success",
                   "url": url, "published_at": published, "mode": mode})
        else:
            error = source_state[1] if source_state and source_state[1] else "TrendForce detail response was not saved"
            status = ("manual_review" if source_state and source_state[0] == "manual_review"
                      else "manual_review" if attempts + 1 >= 3 else "pending")
            db.execute("UPDATE trendforce_detail_jobs SET status=?,attempts=?,last_error=? WHERE url=?",
                       (status, attempts + 1, error[:300], url))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "trendforce_detail_error",
                   "url": url, "error": error[:300]})
        db.close()
        return 1

    jobs = db.execute("""SELECT job_id,run_id,page,mode,attempts FROM trendforce_archive_jobs
        WHERE status='pending' ORDER BY CASE mode WHEN 'backfill' THEN 0 ELSE 1 END,page""").fetchall()
    job = next((row for row in jobs if not active_cooldown(db, SOURCE_ID)), None)
    if not job:
        db.close()
        return 0
    job_id, run_id, page, mode, attempts = job
    url = page_url(page)
    store_search_hint(db, url, page)
    db.commit()
    saved = _saved(db, url)
    reused = bool(saved) and mode == "backfill"
    if not reused:
        collect_source(db, source, user_agent, 1, False, url)
        saved = _saved(db, url)
    try:
        source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                                  (SOURCE_ID,)).fetchone()
        if not saved or not source_state or source_state[0] != "closed":
            raise ValueError(source_state[1] if source_state and source_state[1]
                             else "TrendForce archive response was not saved")
        body = (ROOT / saved[1]).read_bytes()
        dates, items = parse_archive_page(body, url)
        if not dates:
            raise ValueError("TrendForce archive page has no valid dated articles")
        start = str(source.raw["backfill_start"])
        observed = current.isoformat(timespec="seconds")
        in_window = [item for item in items if item.published_at >= start]
        for item in in_window:
            store_candidate(db, source, item, mode, observed)
        skipped = sum(item.published_at < start for item in items)
        has_next = (mode == "backfill" and len(dates) == PAGE_SIZE and
                    any(date >= start for date in dates))
        if has_next and page >= MAX_PAGES:
            raise ValueError("TrendForce archive still in window at page guard")
        if has_next:
            next_page = page + 1
            db.execute("""INSERT OR IGNORE INTO trendforce_archive_jobs
                (job_id,run_id,page,mode) VALUES(?,?,?,?)""",
                (f"{run_id}:{next_page}", run_id, next_page, mode))
        db.execute("""UPDATE trendforce_archive_jobs SET status='done',attempts=?,
            exact_results=?,skipped_before_window=?,last_error=NULL,processed_at=?
            WHERE job_id=?""", (attempts + 1, len(in_window), skipped, observed, job_id))
        remaining = db.execute("""SELECT 1 FROM trendforce_archive_jobs
            WHERE run_id=? AND status!='done' LIMIT 1""", (run_id,)).fetchone()
        pages = db.execute("""SELECT count(*) FROM trendforce_archive_jobs
            WHERE run_id=? AND status='done'""", (run_id,)).fetchone()[0]
        exact = db.execute("SELECT count(*) FROM trendforce_detail_jobs").fetchone()[0]
        skipped_total = db.execute("""SELECT coalesce(sum(skipped_before_window),0)
            FROM trendforce_archive_jobs WHERE run_id=?""", (run_id,)).fetchone()[0]
        db.execute("""UPDATE trendforce_archive_state SET last_completed_at=?,pages_scanned=?,
            exact_results=?,skipped_before_window=?,last_error=NULL WHERE source_id=?""",
            (None if remaining else observed, pages, exact, skipped_total, SOURCE_ID))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "trendforce_archive_page_success",
               "job_id": job_id, "page": page, "visible_articles": len(dates),
               "exact_in_window": len(in_window), "skipped_before_window": skipped,
               "continued": has_next, "reused_saved_response": reused})
    except (ValueError, OSError) as exc:
        source_state = db.execute("SELECT state FROM source_state WHERE source_id=?",
                                  (SOURCE_ID,)).fetchone()
        status = ("manual_review" if source_state and source_state[0] == "manual_review"
                  else "manual_review" if attempts + 1 >= 3 else "pending")
        db.execute("UPDATE trendforce_archive_jobs SET status=?,attempts=?,last_error=? WHERE job_id=?",
                   (status, attempts + 1, str(exc)[:300], job_id))
        db.execute("UPDATE trendforce_archive_state SET last_error=? WHERE source_id=?",
                   (str(exc)[:300], SOURCE_ID))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "trendforce_archive_page_error",
               "job_id": job_id, "page": page, "error": str(exc)[:300]})
    db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    print(f"processed={process_one(contact)}")
