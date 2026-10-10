#!/usr/bin/env python3
"""Daily exact-entity discovery from Ijiwei's public SSR latest-news window."""
from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from collector.collect import (
    ROOT, audit, blocked_host, canonicalize, collect_source, connect, load_sources,
)

SOURCE_ID = "ijiwei"
SEED_URL = "https://www.ijiwei.com/jwnews"
EXPECTED_HOST = "www.ijiwei.com"
DETAIL_RE = re.compile(r"^/n/(\d+)$")
STRONG_RE = re.compile(
    r"长鑫科技(?:集团股份有限公司)?|长鑫存储|(?<![A-Za-z0-9])CXMT(?![A-Za-z0-9])|ChangXin Memory(?: Technologies)?",
    re.I)
CONTEXT_RE = re.compile(r"DRAM|LPDDR|DDR[3456]?|存储|内存|芯片|合肥", re.I)


@dataclass(frozen=True)
class LatestItem:
    url: str
    title: str
    summary: str
    document_type: str = "semiconductor_news"


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def entity_match(text: str) -> bool:
    return bool(STRONG_RE.search(text) or ("长鑫" in text and CONTEXT_RE.search(text)))


def parse_latest(body: bytes, base_url: str = SEED_URL) -> tuple[int, list[LatestItem]]:
    source = body.decode("utf-8", errors="ignore")
    cards = re.findall(r'<li\s+class=["\']card["\'][^>]*?(?:data-href=["\']([^"\']+)["\'])[^>]*>(.*?)</li>',
                       source, re.I | re.S)
    if len(cards) > 50:
        raise ValueError("Ijiwei latest window exceeds 50-card guard")
    items: list[LatestItem] = []
    seen: set[str] = set()
    for href, card in cards:
        url = canonicalize(urljoin(base_url, href))
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != EXPECTED_HOST or
                parsed.query or not DETAIL_RE.fullmatch(parsed.path) or url in seen):
            continue
        title_match = re.search(
            r'<p[^>]+class=["\'][^"\']*\btitle\b[^"\']*["\'][^>]*>(.*?)</p>',
            card, re.I | re.S)
        summary_match = re.search(
            r'<p[^>]+class=["\'][^"\']*\bintro(?:-not)?\b[^"\']*["\'][^>]*>(.*?)</p>',
            card, re.I | re.S)
        title = _text(title_match.group(1)) if title_match else ""
        summary = _text(summary_match.group(1)) if summary_match else ""
        if title and entity_match(title + " " + summary):
            items.append(LatestItem(url, title, summary))
        seen.add(url)
    return len(seen), items


def parse_detail_metadata(body: bytes) -> tuple[str, str]:
    source = body.decode("utf-8", errors="ignore")
    title_match = re.search(
        r'<h1[^>]+class=["\'][^"\']*\bmedia-title\b[^"\']*["\'][^>]*>(.*?)</h1>',
        source, re.I | re.S)
    if not title_match:
        title_match = re.search(r'<title[^>]*>(.*?)</title>', source, re.I | re.S)
    title = _text(title_match.group(1)) if title_match else ""
    date_match = re.search(r'"published_time"\s*:\s*"([12]\d{7})\d{6}"', source)
    published = (f"{date_match.group(1)[:4]}-{date_match.group(1)[4:6]}-"
                 f"{date_match.group(1)[6:8]}" if date_match else "")
    try:
        datetime.strptime(published, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Ijiwei detail is missing a valid publication date") from exc
    if not title:
        raise ValueError("Ijiwei detail is missing a title")
    return title, published


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS ijiwei_latest_state (
        source_id TEXT PRIMARY KEY, seed_content_hash TEXT,
        last_seed_fetched_at TEXT, last_scanned_at TEXT,
        visible_items INTEGER NOT NULL DEFAULT 0,
        exact_candidates INTEGER NOT NULL DEFAULT 0,
        coverage_note TEXT NOT NULL DEFAULT 'latest_20_only_no_historical_api',
        last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS ijiwei_detail_jobs (
        url TEXT PRIMARY KEY,title TEXT NOT NULL,summary TEXT,
        document_type TEXT NOT NULL,mode TEXT NOT NULL DEFAULT 'incremental',
        status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,processed_at TEXT,first_seen_at TEXT NOT NULL)""")
    db.execute("INSERT OR IGNORE INTO ijiwei_latest_state(source_id) VALUES(?)", (SOURCE_ID,))
    db.commit()


def _saved(db: sqlite3.Connection, url: str):
    return db.execute("""SELECT content_hash,raw_path,fetched_at,status FROM documents
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url))).fetchone()


def store_seed_hint(db: sqlite3.Connection) -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,document_type,review_status)
        VALUES(?,?,'爱集微最新资讯窗口','search_response','search_response')
        ON CONFLICT(source_id,canonical_url) DO UPDATE SET title=excluded.title,
        document_type='search_response',review_status='search_response'""",
        (SOURCE_ID, SEED_URL))
    db.execute("UPDATE documents SET review_status='search_response' WHERE source_id=? AND canonical_url=?",
               (SOURCE_ID, SEED_URL))
    db.execute("""UPDATE document_versions SET document_type='search_response',
        review_status='search_response',company_id=NULL,matched_legal_entity=NULL
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, SEED_URL))


def store_item(db: sqlite3.Connection, source, item: LatestItem, observed: str) -> None:
    legal_name = source.raw.get("_canonical_names", {}).get("cxmt")
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,'cxmt',?,?,?,'candidate_entity_match')
        ON CONFLICT(source_id,canonical_url) DO UPDATE SET title=excluded.title,
        company_id=excluded.company_id,matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, item.url, item.title, legal_name, item.document_type, "Asia/Shanghai"))
    db.execute("""INSERT INTO ijiwei_detail_jobs
        (url,title,summary,document_type,first_seen_at) VALUES(?,?,?,?,?)
        ON CONFLICT(url) DO UPDATE SET title=excluded.title,summary=excluded.summary,
        document_type=excluded.document_type""",
        (item.url, item.title, item.summary, item.document_type, observed))


def refresh_saved_seed(db: sqlite3.Connection, source,
                       now: datetime | None = None) -> int:
    row = _saved(db, SEED_URL)
    if not row:
        return 0
    store_seed_hint(db)
    db.commit()
    state = db.execute("SELECT seed_content_hash FROM ijiwei_latest_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    if state and state[0] == row[0]:
        return 0
    path = ROOT / row[1]
    if not path.is_file():
        raise ValueError("Ijiwei latest raw file is missing")
    visible, items = parse_latest(path.read_bytes())
    if not visible:
        raise ValueError("Ijiwei latest page has no article cards")
    observed = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    before = db.execute("SELECT count(*) FROM ijiwei_detail_jobs").fetchone()[0]
    for item in items:
        store_item(db, source, item, observed)
    after = db.execute("SELECT count(*) FROM ijiwei_detail_jobs").fetchone()[0]
    db.execute("""UPDATE ijiwei_latest_state SET seed_content_hash=?,last_seed_fetched_at=?,
        last_scanned_at=?,visible_items=?,exact_candidates=?,last_error=NULL
        WHERE source_id=?""", (row[0], row[2], observed, visible, len(items), SOURCE_ID))
    db.commit()
    audit({"source_id": SOURCE_ID, "outcome": "ijiwei_latest_seed_scanned",
           "visible_items": visible, "exact_candidates": len(items),
           "new_jobs": after - before, "coverage_note": "latest_20_only_no_historical_api"})
    return after - before


def seed_due(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    row = db.execute("SELECT last_seed_fetched_at FROM ijiwei_latest_state WHERE source_id=?",
                     (SOURCE_ID,)).fetchone()
    if not row or not row[0]:
        return True
    return datetime.fromisoformat(row[0]) + timedelta(days=1) <= (
        now or datetime.now(timezone.utc))


def suspend_pending(db: sqlite3.Connection, reason: str) -> None:
    changed = db.execute("""UPDATE ijiwei_detail_jobs SET status='manual_review',last_error=?
        WHERE status='pending'""", (reason,)).rowcount
    db.execute("UPDATE ijiwei_latest_state SET last_error=? WHERE source_id=?",
               (reason, SOURCE_ID))
    db.commit()
    if changed:
        audit({"source_id": SOURCE_ID, "outcome": "ijiwei_latest_suspended_manual_review",
               "affected_jobs": changed, "error": reason})


def process_one(contact: str, now: datetime | None = None) -> int:
    _, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    current = now or datetime.now(timezone.utc)
    try:
        refresh_saved_seed(db, source, current)
    except (ValueError, OSError) as exc:
        db.execute("UPDATE ijiwei_latest_state SET last_error=? WHERE source_id=?",
                   (str(exc)[:300], SOURCE_ID))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "ijiwei_latest_seed_error",
               "error": str(exc)[:300]})
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    reason = blocked_host(db, SOURCE_ID, EXPECTED_HOST)
    if reason or (source_state and source_state[0] == "manual_review"):
        suspend_pending(db, reason or source_state[1] or "source requires manual review")
        db.close()
        return 0
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    job = db.execute("""SELECT url,title,summary,document_type,mode,attempts
        FROM ijiwei_detail_jobs WHERE status='pending' ORDER BY first_seen_at,url LIMIT 1""").fetchone()
    if job:
        url, title, summary, kind, mode, attempts = job
        item = LatestItem(url, title, summary or "", kind)
        store_item(db, source, item, current.isoformat(timespec="seconds"))
        db.commit()
        collect_source(db, source, user_agent, 1, False, url)
        saved = _saved(db, url)
        source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                                  (SOURCE_ID,)).fetchone()
        try:
            if not saved or not source_state or source_state[0] != "closed":
                raise ValueError(source_state[1] if source_state and source_state[1]
                                 else "Ijiwei detail response was not saved")
            actual_title, published = parse_detail_metadata((ROOT / saved[1]).read_bytes())
            status = "done" if published >= str(source.raw["backfill_start"]) else "skipped_before_window"
            legal_name = source.raw.get("_canonical_names", {}).get("cxmt")
            db.execute("""UPDATE discovered_metadata SET title=?,published_at=?,company_id='cxmt',
                matched_legal_entity=?,document_type=?,review_status='candidate_entity_match'
                WHERE source_id=? AND canonical_url=?""",
                (actual_title, published, legal_name, kind, SOURCE_ID, url))
            db.execute("UPDATE documents SET title=?,review_status='candidate_entity_match' WHERE source_id=? AND canonical_url=?",
                       (actual_title, SOURCE_ID, url))
            db.execute("""UPDATE document_versions SET title=?,published_at=?,published_basis='page_metadata',
                company_id='cxmt',matched_legal_entity=?,document_type=?,review_status='candidate_entity_match'
                WHERE source_id=? AND canonical_url=?""",
                (actual_title, published, legal_name, kind, SOURCE_ID, url))
            db.execute("""UPDATE ijiwei_detail_jobs SET title=?,status=?,attempts=?,
                last_error=NULL,processed_at=? WHERE url=?""",
                (actual_title, status, attempts + 1, current.isoformat(timespec="seconds"), url))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "ijiwei_detail_success",
                   "url": url, "published_at": published, "status": status})
        except (ValueError, OSError) as exc:
            status = ("manual_review" if source_state and source_state[0] == "manual_review"
                      else "manual_review" if attempts + 1 >= 3 else "pending")
            db.execute("UPDATE ijiwei_detail_jobs SET status=?,attempts=?,last_error=? WHERE url=?",
                       (status, attempts + 1, str(exc)[:300], url))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "ijiwei_detail_error",
                   "url": url, "error": str(exc)[:300]})
        db.close()
        return 1
    if seed_due(db, current):
        store_seed_hint(db)
        db.commit()
        collect_source(db, source, user_agent, 1, False, SEED_URL)
        try:
            refresh_saved_seed(db, source, current)
        except (ValueError, OSError) as exc:
            db.execute("UPDATE ijiwei_latest_state SET last_error=? WHERE source_id=?",
                       (str(exc)[:300], SOURCE_ID))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "ijiwei_latest_seed_error",
                   "error": str(exc)[:300]})
        db.close()
        return 1
    db.close()
    return 0


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    print(f"processed={process_one(contact)}")
