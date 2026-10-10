#!/usr/bin/env python3
"""Title-level CXMT discovery from Tom's Hardware's robots-listed sitemaps."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

from collector.biospace_sitemap import parse_detail_metadata
from collector.collect import (
    ROOT, audit, blocked_host, canonicalize, collect_source, connect, load_sources,
)

SOURCE_ID = "toms_hardware"
INDEX_URL = "https://www.tomshardware.com/sitemap.xml"
EXPECTED_HOST = "www.tomshardware.com"
MONTH_RE = re.compile(r"^/sitemap-((?:19|20)\d{2})-(0[1-9]|1[0-2])\.xml$")
ENTITY_SLUG_RE = re.compile(
    r"(?:^|-)(?:cxmt|changxin-memory-technologies)(?:-|$)", re.I)


@dataclass(frozen=True)
class SitemapCandidate:
    url: str
    title: str
    sitemap_month: str
    document_type: str = "hardware_news"


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS toms_sitemap_state (
        source_id TEXT PRIMARY KEY, index_content_hash TEXT,
        last_index_fetched_at TEXT, last_completed_at TEXT,
        coverage_start_month TEXT, coverage_end_month TEXT,
        months_scanned INTEGER NOT NULL DEFAULT 0,
        exact_candidates INTEGER NOT NULL DEFAULT 0,
        coverage_note TEXT NOT NULL DEFAULT 'title_url_only', last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS toms_sitemap_jobs (
        sitemap_month TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE,
        mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, total_urls INTEGER,
        exact_candidates INTEGER NOT NULL DEFAULT 0, last_error TEXT,
        processed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS toms_detail_jobs (
        url TEXT PRIMARY KEY, title TEXT NOT NULL, sitemap_month TEXT NOT NULL,
        document_type TEXT NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT, first_seen_at TEXT NOT NULL)""")
    db.execute("""INSERT OR IGNORE INTO toms_sitemap_state
        (source_id,coverage_start_month) VALUES(?, '2024-01')""", (SOURCE_ID,))
    db.commit()


def _root(body: bytes) -> ElementTree.Element:
    if len(body) > 20_000_000:
        raise ValueError("Tom's Hardware sitemap exceeds 20 MB guard")
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ValueError(f"invalid Tom's Hardware sitemap XML: {exc}") from exc


def parse_sitemap_index(body: bytes, start_month: str,
                        end_month: str) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for node in _root(body).iter():
        if node.tag.rsplit("}", 1)[-1] != "loc" or not node.text:
            continue
        url = canonicalize(node.text.strip())
        parsed = urlparse(url)
        match = MONTH_RE.fullmatch(parsed.path)
        if (parsed.scheme != "https" or parsed.hostname != EXPECTED_HOST or
                parsed.query or not match):
            continue
        month = f"{match.group(1)}-{match.group(2)}"
        if start_month <= month <= end_month:
            found[month] = url
    return sorted(found.items(), reverse=True)


def parse_month_sitemap(body: bytes, month: str) -> tuple[int, list[SitemapCandidate]]:
    urls: list[str] = []
    for entry in _root(body):
        if entry.tag.rsplit("}", 1)[-1] != "url":
            continue
        node = next((child for child in entry
                     if child.tag.rsplit("}", 1)[-1] == "loc"), None)
        if node is not None and node.text:
            urls.append(canonicalize(node.text.strip()))
    if len(urls) > 100_000:
        raise ValueError("Tom's Hardware monthly sitemap exceeds 100000 URL guard")
    candidates: list[SitemapCandidate] = []
    seen: set[str] = set()
    for url in urls:
        parsed = urlparse(url)
        slug = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
        if (parsed.scheme != "https" or parsed.hostname != EXPECTED_HOST or
                parsed.query or not ENTITY_SLUG_RE.search(slug) or url in seen):
            continue
        candidates.append(SitemapCandidate(
            url, re.sub(r"\s+", " ", slug.replace("-", " ")).strip(), month))
        seen.add(url)
    return len(urls), candidates


def _saved(db: sqlite3.Connection, url: str):
    return db.execute("""SELECT content_hash,raw_path,fetched_at,status FROM documents
        WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url))).fetchone()


def _body(row) -> bytes:
    if not row or int(row[3]) != 200 or not row[1]:
        raise ValueError("Tom's Hardware sitemap response was not saved")
    path = ROOT / row[1]
    if not path.is_file():
        raise ValueError("Tom's Hardware sitemap raw file is missing")
    return path.read_bytes()


def store_search_hint(db: sqlite3.Connection, url: str, title: str) -> None:
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


def store_candidate_hint(db: sqlite3.Connection, source, item: SitemapCandidate) -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,'cxmt',?,?,?,'candidate_entity_match')
        ON CONFLICT(source_id,canonical_url) DO UPDATE SET title=excluded.title,
        company_id=excluded.company_id,matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, item.url, item.title,
         source.raw.get("_canonical_names", {}).get("cxmt"),
         item.document_type, "America/New_York"))


def ingest_saved_index(db: sqlite3.Connection, source,
                       now: datetime | None = None) -> int:
    row = _saved(db, INDEX_URL)
    if not row:
        return 0
    current = now or datetime.now(timezone.utc)
    state = db.execute("SELECT index_content_hash FROM toms_sitemap_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    if state and state[0] == row[0]:
        return 0
    current_month = current.strftime("%Y-%m")
    start_month = str(source.raw["backfill_start"])[:7]
    months = parse_sitemap_index(_body(row), start_month, current_month)
    if not months:
        raise ValueError("Tom's Hardware index has no in-window monthly sitemaps")
    initial = not state or not state[0]
    inserted = 0
    for month, url in months:
        existing = db.execute("SELECT status FROM toms_sitemap_jobs WHERE sitemap_month=?",
                              (month,)).fetchone()
        if not existing:
            db.execute("INSERT INTO toms_sitemap_jobs(sitemap_month,url,mode) VALUES(?,?,?)",
                       (month, url, "backfill" if initial else "incremental"))
            inserted += 1
        elif not initial and month == current_month and existing[0] == "done":
            db.execute("""UPDATE toms_sitemap_jobs SET status='pending',mode='incremental',
                attempts=0,last_error=NULL,processed_at=NULL WHERE sitemap_month=?""", (month,))
    db.execute("""UPDATE toms_sitemap_state SET index_content_hash=?,last_index_fetched_at=?,
        coverage_start_month=?,coverage_end_month=?,last_error=NULL WHERE source_id=?""",
        (row[0], row[2], months[-1][0], months[0][0], SOURCE_ID))
    db.commit()
    audit({"source_id": SOURCE_ID, "outcome": "toms_sitemap_index_scanned",
           "months_visible": len(months), "new_month_jobs": inserted,
           "coverage_start_month": months[-1][0], "coverage_end_month": months[0][0]})
    return inserted


def _update_progress(db: sqlite3.Connection, observed: str) -> None:
    months = db.execute("SELECT count(*) FROM toms_sitemap_jobs WHERE status='done'").fetchone()[0]
    exact = db.execute("SELECT count(*) FROM toms_detail_jobs").fetchone()[0]
    incomplete = db.execute("SELECT 1 FROM toms_sitemap_jobs WHERE status!='done' LIMIT 1").fetchone()
    db.execute("""UPDATE toms_sitemap_state SET last_completed_at=?,months_scanned=?,
        exact_candidates=?,last_error=NULL WHERE source_id=?""",
        (None if incomplete else observed, months, exact, SOURCE_ID))


def index_due(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    row = db.execute("SELECT last_index_fetched_at FROM toms_sitemap_state WHERE source_id=?",
                     (SOURCE_ID,)).fetchone()
    if not row or not row[0]:
        return True
    return datetime.fromisoformat(row[0]) + timedelta(days=7) <= (
        now or datetime.now(timezone.utc))


def suspend_pending(db: sqlite3.Connection, reason: str) -> None:
    changed = 0
    for table in ("toms_sitemap_jobs", "toms_detail_jobs"):
        changed += db.execute(
            f"UPDATE {table} SET status='manual_review',last_error=? WHERE status='pending'",
            (reason,)).rowcount
    db.execute("UPDATE toms_sitemap_state SET last_error=? WHERE source_id=?",
               (reason, SOURCE_ID))
    db.commit()
    if changed:
        audit({"source_id": SOURCE_ID, "outcome": "toms_sitemap_suspended_manual_review",
               "affected_jobs": changed, "error": reason})


def process_one(contact: str, now: datetime | None = None) -> int:
    _, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    current = now or datetime.now(timezone.utc)
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    try:
        ingest_saved_index(db, source, current)
    except (ValueError, OSError) as exc:
        db.execute("UPDATE toms_sitemap_state SET last_error=? WHERE source_id=?",
                   (str(exc)[:300], SOURCE_ID))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "toms_sitemap_index_error",
               "error": str(exc)[:300]})
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    reason = blocked_host(db, SOURCE_ID, EXPECTED_HOST)
    if reason or (source_state and source_state[0] == "manual_review"):
        suspend_pending(db, reason or source_state[1] or "source requires manual review")
        db.close()
        return 0

    detail = db.execute("""SELECT url,title,sitemap_month,document_type,mode,attempts
        FROM toms_detail_jobs WHERE status='pending' ORDER BY sitemap_month DESC,url LIMIT 1""").fetchone()
    if detail:
        url, title, month, kind, mode, attempts = detail
        item = SitemapCandidate(url, title, month, kind)
        store_candidate_hint(db, source, item)
        db.commit()
        collect_source(db, source, user_agent, 1, False, url)
        saved = _saved(db, url)
        source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                                  (SOURCE_ID,)).fetchone()
        try:
            if not saved or not source_state or source_state[0] != "closed":
                raise ValueError(source_state[1] if source_state and source_state[1]
                                 else "Tom's Hardware detail response was not saved")
            actual_title, published = parse_detail_metadata(_body(saved))
            legal_name = source.raw.get("_canonical_names", {}).get("cxmt")
            db.execute("""UPDATE discovered_metadata SET title=?,published_at=?,company_id='cxmt',
                matched_legal_entity=?,document_type=?,review_status='candidate_entity_match'
                WHERE source_id=? AND canonical_url=?""",
                (actual_title, published, legal_name, kind, SOURCE_ID, url))
            db.execute("UPDATE documents SET title=?,review_status='candidate_entity_match' WHERE source_id=? AND canonical_url=?",
                       (actual_title, SOURCE_ID, url))
            db.execute("""UPDATE document_versions SET title=?,published_at=?,
                published_basis='page_metadata',company_id='cxmt',matched_legal_entity=?,
                document_type=?,review_status='candidate_entity_match'
                WHERE source_id=? AND canonical_url=?""",
                (actual_title, published, legal_name, kind, SOURCE_ID, url))
            status = "done" if published >= str(source.raw["backfill_start"]) else "skipped_before_window"
            processed = current.isoformat(timespec="seconds")
            db.execute("""UPDATE toms_detail_jobs SET title=?,status=?,attempts=?,last_error=NULL,
                processed_at=? WHERE url=?""", (actual_title, status, attempts + 1, processed, url))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "toms_detail_success", "url": url,
                   "published_at": published, "status": status, "mode": mode})
        except (ValueError, OSError) as exc:
            status = ("manual_review" if source_state and source_state[0] == "manual_review"
                      else "manual_review" if attempts + 1 >= 3 else "pending")
            db.execute("UPDATE toms_detail_jobs SET status=?,attempts=?,last_error=? WHERE url=?",
                       (status, attempts + 1, str(exc)[:300], url))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "toms_detail_error",
                   "url": url, "error": str(exc)[:300]})
        db.close()
        return 1

    job = db.execute("""SELECT sitemap_month,url,mode,attempts FROM toms_sitemap_jobs
        WHERE status='pending' ORDER BY sitemap_month DESC LIMIT 1""").fetchone()
    if job:
        month, url, mode, attempts = job
        store_search_hint(db, url, f"Tom's Hardware monthly sitemap {month}")
        db.commit()
        saved = _saved(db, url)
        reused = bool(saved) and mode == "backfill"
        if not reused:
            collect_source(db, source, user_agent, 1, False, url)
            saved = _saved(db, url)
        try:
            source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                                      (SOURCE_ID,)).fetchone()
            if not source_state or source_state[0] != "closed":
                raise ValueError(source_state[1] if source_state and source_state[1]
                                 else "Tom's Hardware sitemap source is not closed")
            total, candidates = parse_month_sitemap(_body(saved), month)
            observed = current.isoformat(timespec="seconds")
            for item in candidates:
                store_candidate_hint(db, source, item)
                db.execute("""INSERT INTO toms_detail_jobs
                    (url,title,sitemap_month,document_type,mode,first_seen_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                    sitemap_month=excluded.sitemap_month,document_type=excluded.document_type""",
                    (item.url, item.title, month, item.document_type, mode, observed))
            db.execute("""UPDATE toms_sitemap_jobs SET status='done',attempts=?,total_urls=?,
                exact_candidates=?,last_error=NULL,processed_at=? WHERE sitemap_month=?""",
                (attempts + 1, total, len(candidates), observed, month))
            _update_progress(db, observed)
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "toms_sitemap_month_success",
                   "sitemap_month": month, "total_urls": total,
                   "exact_candidates": len(candidates), "mode": mode,
                   "reused_saved_response": reused})
        except (ValueError, OSError) as exc:
            source_state = db.execute("SELECT state FROM source_state WHERE source_id=?",
                                      (SOURCE_ID,)).fetchone()
            status = ("manual_review" if source_state and source_state[0] == "manual_review"
                      else "manual_review" if attempts + 1 >= 3 else "pending")
            db.execute("UPDATE toms_sitemap_jobs SET status=?,attempts=?,last_error=? WHERE sitemap_month=?",
                       (status, attempts + 1, str(exc)[:300], month))
            db.execute("UPDATE toms_sitemap_state SET last_error=? WHERE source_id=?",
                       (str(exc)[:300], SOURCE_ID))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "toms_sitemap_month_error",
                   "sitemap_month": month, "error": str(exc)[:300]})
        db.close()
        return 1

    if index_due(db, current):
        store_search_hint(db, INDEX_URL, "Tom's Hardware sitemap index")
        db.commit()
        collect_source(db, source, user_agent, 1, False, INDEX_URL)
        try:
            ingest_saved_index(db, source, current)
        except (ValueError, OSError) as exc:
            db.execute("UPDATE toms_sitemap_state SET last_error=? WHERE source_id=?",
                       (str(exc)[:300], SOURCE_ID))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "toms_sitemap_index_error",
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
