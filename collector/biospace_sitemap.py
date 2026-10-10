#!/usr/bin/env python3
"""Robots-compliant, title-level BioSpace discovery through public sitemaps."""
from __future__ import annotations

import re
import sqlite3
import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

from collector.collect import (
    ROOT, audit, blocked_host, canonicalize, collect_source, connect, load_sources, meta_content,
)

SOURCE_ID = "biospace"
INDEX_URL = "https://www.biospace.com/sitemap.xml"
EXPECTED_HOST = "www.biospace.com"
MONTH_RE = re.compile(r"^/sitemap-((?:19|20)\d{2}(?:0[1-9]|1[0-2]))\.xml$")
EXACT_SLUG_RE = re.compile(r"(?:^|-)wuxi-apptec(?:-|$)", re.I)


@dataclass(frozen=True)
class SitemapCandidate:
    url: str
    title: str
    sitemap_month: str
    document_type: str


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS biospace_sitemap_state (
        source_id TEXT PRIMARY KEY, index_content_hash TEXT,
        last_index_fetched_at TEXT, last_completed_at TEXT,
        coverage_start_month TEXT, coverage_end_month TEXT,
        months_scanned INTEGER NOT NULL DEFAULT 0,
        exact_candidates INTEGER NOT NULL DEFAULT 0,
        coverage_note TEXT NOT NULL DEFAULT 'title_url_only', last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS biospace_sitemap_jobs (
        sitemap_month TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE,
        mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, total_urls INTEGER,
        exact_candidates INTEGER NOT NULL DEFAULT 0, last_error TEXT,
        processed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS biospace_detail_jobs (
        url TEXT PRIMARY KEY, title TEXT NOT NULL, sitemap_month TEXT NOT NULL,
        document_type TEXT NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT, first_seen_at TEXT NOT NULL)""")
    db.execute("""INSERT OR IGNORE INTO biospace_sitemap_state
        (source_id,coverage_start_month) VALUES(?, '2024-01')""", (SOURCE_ID,))
    db.commit()


def _xml_root(body: bytes) -> ElementTree.Element:
    if len(body) > 20_000_000:
        raise ValueError("BioSpace sitemap exceeds 20 MB guard")
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ValueError(f"invalid BioSpace sitemap XML: {exc}") from exc


def parse_sitemap_index(body: bytes, start_month: str,
                        end_month: str) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for node in _xml_root(body).iter():
        if node.tag.rsplit("}", 1)[-1] != "loc" or not node.text:
            continue
        url = canonicalize(node.text.strip())
        parsed = urlparse(url)
        match = MONTH_RE.fullmatch(parsed.path)
        if (parsed.scheme != "https" or parsed.hostname != EXPECTED_HOST or
                parsed.query or not match):
            continue
        compact = match.group(1)
        month = f"{compact[:4]}-{compact[4:]}"
        if start_month <= month <= end_month:
            found[month] = url
    return sorted(found.items(), reverse=True)


def _slug_title(path: str) -> str:
    slug = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return re.sub(r"\s+", " ", slug.replace("-", " ")).strip()


def parse_month_sitemap(body: bytes, sitemap_month: str) -> tuple[int, list[SitemapCandidate]]:
    urls: list[str] = []
    for entry in _xml_root(body):
        if entry.tag.rsplit("}", 1)[-1] != "url":
            continue
        node = next((child for child in entry
                     if child.tag.rsplit("}", 1)[-1] == "loc"), None)
        if node is not None and node.text:
            urls.append(canonicalize(node.text.strip()))
    if len(urls) > 100_000:
        raise ValueError("BioSpace monthly sitemap exceeds 100000 URL guard")
    candidates: list[SitemapCandidate] = []
    seen: set[str] = set()
    for url in urls:
        parsed = urlparse(url)
        slug = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
        if (parsed.scheme != "https" or parsed.hostname != EXPECTED_HOST or
                parsed.query or not EXACT_SLUG_RE.search(slug) or url in seen):
            continue
        kind = "press_releases" if parsed.path.startswith("/press-releases/") else "industry_news"
        candidates.append(SitemapCandidate(
            url=url, title=_slug_title(parsed.path), sitemap_month=sitemap_month,
            document_type=kind))
        seen.add(url)
    return len(urls), candidates


def parse_detail_metadata(body: bytes) -> tuple[str, str]:
    text = body.decode("utf-8", errors="ignore")
    title = meta_content(text, "og:title") or ""
    if not title:
        heading = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.I | re.S)
        title = (re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", heading.group(1)))).strip()
                 if heading else "")
    date_match = re.search(
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
        text, re.I)
    if not date_match:
        date_match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', text, re.I)
    published_at = date_match.group(1)[:10] if date_match else ""
    try:
        datetime.strptime(published_at, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("BioSpace detail is missing a valid publication date") from exc
    if not title:
        raise ValueError("BioSpace detail is missing a title")
    return title, published_at


def apply_detail_metadata(db: sqlite3.Connection, source, item: SitemapCandidate,
                          raw_path: str) -> tuple[str, str]:
    title, published_at = parse_detail_metadata((ROOT / raw_path).read_bytes())
    legal_name = source.raw.get("_canonical_names", {}).get("wuxi_apptec")
    db.execute("""UPDATE discovered_metadata SET title=?,published_at=?,company_id='wuxi_apptec',
        matched_legal_entity=?,document_type=?,original_timezone='America/New_York',
        review_status='candidate_entity_match' WHERE source_id=? AND canonical_url=?""",
        (title, published_at, legal_name, item.document_type, SOURCE_ID, item.url))
    db.execute("""UPDATE documents SET title=?,review_status='candidate_entity_match'
        WHERE source_id=? AND canonical_url=?""", (title, SOURCE_ID, item.url))
    db.execute("""UPDATE document_versions SET title=?,published_at=?,published_basis='page_metadata',
        company_id='wuxi_apptec',matched_legal_entity=?,document_type=?,
        review_status='candidate_entity_match' WHERE source_id=? AND canonical_url=?""",
        (title, published_at, legal_name, item.document_type, SOURCE_ID, item.url))
    return title, published_at


def _saved_document(db: sqlite3.Connection, url: str):
    return db.execute("""SELECT content_hash,raw_path,fetched_at,status
        FROM documents WHERE source_id=? AND canonical_url=?""",
        (SOURCE_ID, canonicalize(url))).fetchone()


def _read_saved(row) -> bytes:
    if not row or int(row[3]) != 200 or not row[1]:
        raise ValueError("BioSpace sitemap response was not saved")
    path = ROOT / row[1]
    if not path.is_file():
        raise ValueError("BioSpace sitemap raw file is missing")
    return path.read_bytes()


def store_search_hint(db: sqlite3.Connection, url: str, title: str) -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,document_type,review_status)
        VALUES(?,?,?,'search_response','search_response')
        ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,document_type='search_response',review_status='search_response'""",
        (SOURCE_ID, canonicalize(url), title))
    # A sitemap fetched during adapter discovery is still a query/index response,
    # not an entity event. Correct any already-saved probe in place.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'").fetchone():
        db.execute("""UPDATE documents SET review_status='search_response'
            WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url)))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_versions'").fetchone():
        db.execute("""UPDATE document_versions SET document_type='search_response',
            review_status='search_response',company_id=NULL,matched_legal_entity=NULL
            WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url)))


def store_candidate_hint(db: sqlite3.Connection, source, item: SitemapCandidate) -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,'wuxi_apptec',?,?,?,'candidate_entity_match')
        ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, item.url, item.title,
         source.raw.get("_canonical_names", {}).get("wuxi_apptec"),
         item.document_type, "America/New_York"))


def ingest_saved_index(db: sqlite3.Connection, source,
                       now: datetime | None = None) -> int:
    row = _saved_document(db, INDEX_URL)
    if not row:
        return 0
    current = now or datetime.now(timezone.utc)
    current_month = current.strftime("%Y-%m")
    start_month = str(source.raw["backfill_start"])[:7]
    state = db.execute("""SELECT index_content_hash FROM biospace_sitemap_state
        WHERE source_id=?""", (SOURCE_ID,)).fetchone()
    if state and state[0] == row[0]:
        return 0
    months = parse_sitemap_index(_read_saved(row), start_month, current_month)
    if not months:
        raise ValueError("BioSpace sitemap index has no in-window monthly sitemaps")
    initial = not state or not state[0]
    inserted = 0
    for month, url in months:
        exists = db.execute("SELECT status FROM biospace_sitemap_jobs WHERE sitemap_month=?",
                            (month,)).fetchone()
        if not exists:
            db.execute("""INSERT INTO biospace_sitemap_jobs
                (sitemap_month,url,mode) VALUES(?,?,?)""",
                (month, url, "backfill" if initial else "incremental"))
            inserted += 1
        elif not initial and month == current_month and exists[0] == "done":
            db.execute("""UPDATE biospace_sitemap_jobs SET status='pending',mode='incremental',
                attempts=0,last_error=NULL,processed_at=NULL WHERE sitemap_month=?""", (month,))
    observed = current.isoformat(timespec="seconds")
    db.execute("""UPDATE biospace_sitemap_state SET index_content_hash=?,
        last_index_fetched_at=?,coverage_start_month=?,coverage_end_month=?,last_error=NULL
        WHERE source_id=?""", (row[0], row[2], months[-1][0], months[0][0], SOURCE_ID))
    db.commit()
    audit({"source_id": SOURCE_ID, "outcome": "biospace_sitemap_index_scanned",
           "months_visible": len(months), "new_month_jobs": inserted,
           "coverage_start_month": months[-1][0], "coverage_end_month": months[0][0],
           "observed_at": observed})
    return inserted


def index_fetch_due(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    row = db.execute("""SELECT last_index_fetched_at FROM biospace_sitemap_state
        WHERE source_id=?""", (SOURCE_ID,)).fetchone()
    if not row or not row[0]:
        return True
    current = now or datetime.now(timezone.utc)
    return datetime.fromisoformat(row[0]) + timedelta(days=7) <= current


def _finish_state_if_complete(db: sqlite3.Connection, observed: str) -> None:
    months = db.execute("SELECT count(*) FROM biospace_sitemap_jobs WHERE status='done'").fetchone()[0]
    exact = db.execute("SELECT count(*) FROM biospace_detail_jobs").fetchone()[0]
    incomplete = db.execute(
        "SELECT 1 FROM biospace_sitemap_jobs WHERE status!='done' LIMIT 1").fetchone()
    db.execute("""UPDATE biospace_sitemap_state SET last_completed_at=?,months_scanned=?,
        exact_candidates=?,last_error=NULL WHERE source_id=?""",
        (None if incomplete else observed, months, exact, SOURCE_ID))


def suspend_pending(db: sqlite3.Connection, reason: str) -> int:
    changed = 0
    for table in ("biospace_sitemap_jobs", "biospace_detail_jobs"):
        changed += db.execute(
            f"UPDATE {table} SET status='manual_review',last_error=? WHERE status='pending'",
            (reason,)).rowcount
    db.execute("UPDATE biospace_sitemap_state SET last_error=? WHERE source_id=?",
               (reason, SOURCE_ID))
    db.commit()
    if changed:
        audit({"source_id": SOURCE_ID,
               "outcome": "biospace_sitemap_suspended_manual_review",
               "affected_jobs": changed, "error": reason})
    return changed


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
    except ValueError as exc:
        db.execute("UPDATE biospace_sitemap_state SET last_error=? WHERE source_id=?",
                   (str(exc)[:300], SOURCE_ID))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "biospace_sitemap_index_error",
               "error": str(exc)[:300]})

    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    reason = blocked_host(db, SOURCE_ID, EXPECTED_HOST)
    if reason or (state and state[0] == "manual_review"):
        suspend_pending(db, reason or state[1] or "source requires manual review")
        db.close()
        return 0

    detail = db.execute("""SELECT url,title,sitemap_month,document_type,mode,attempts
        FROM biospace_detail_jobs WHERE status='pending'
        ORDER BY sitemap_month DESC,url LIMIT 1""").fetchone()
    if detail:
        url, title, month, document_type, mode, attempts = detail
        item = SitemapCandidate(url, title, month, document_type)
        store_candidate_hint(db, source, item)
        db.commit()
        collect_source(db, source, user_agent, 1, False, url)
        saved = db.execute("""SELECT status,raw_path FROM documents
            WHERE source_id=? AND canonical_url=?""", (SOURCE_ID, canonicalize(url))).fetchone()
        source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                                  (SOURCE_ID,)).fetchone()
        if saved and int(saved[0]) == 200 and saved[1] and source_state and source_state[0] == "closed":
            try:
                actual_title, published_at = apply_detail_metadata(db, source, item, saved[1])
                processed = current.isoformat(timespec="seconds")
                status = ("done" if published_at >= str(source.raw["backfill_start"])
                          else "skipped_before_window")
                db.execute("""UPDATE biospace_detail_jobs SET title=?,status=?,attempts=?,
                    last_error=NULL,processed_at=? WHERE url=?""",
                    (actual_title, status, attempts + 1, processed, url))
                db.commit()
                audit({"source_id": SOURCE_ID, "outcome": "biospace_detail_success",
                       "url": url, "sitemap_month": month, "published_at": published_at,
                       "document_type": document_type, "mode": mode,
                       "status": status})
            except (ValueError, OSError) as exc:
                db.execute("""UPDATE biospace_detail_jobs SET status=?,attempts=?,last_error=?
                    WHERE url=?""", ("manual_review" if attempts + 1 >= 3 else "pending",
                                      attempts + 1, str(exc)[:300], url))
                db.commit()
                audit({"source_id": SOURCE_ID, "outcome": "biospace_detail_error",
                       "url": url, "error": str(exc)[:300]})
        else:
            error = (source_state[1] if source_state and source_state[1]
                     else "BioSpace detail response was not saved")
            status = ("manual_review" if source_state and source_state[0] == "manual_review"
                      else "manual_review" if attempts + 1 >= 3 else "pending")
            db.execute("""UPDATE biospace_detail_jobs SET status=?,attempts=?,last_error=?
                WHERE url=?""", (status, attempts + 1, error[:300], url))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "biospace_detail_error",
                   "url": url, "error": error[:300]})
        db.close()
        return 1

    job = db.execute("""SELECT sitemap_month,url,mode,attempts FROM biospace_sitemap_jobs
        WHERE status='pending' ORDER BY sitemap_month DESC LIMIT 1""").fetchone()
    if job:
        month, url, mode, attempts = job
        store_search_hint(db, url, f"BioSpace monthly sitemap {month}")
        db.commit()
        # Reuse a successful probe or prior weekly copy; otherwise fetch once.
        saved = _saved_document(db, url)
        reused_saved = bool(saved) and mode == "backfill"
        if not reused_saved:
            collect_source(db, source, user_agent, 1, False, url)
            saved = _saved_document(db, url)
        try:
            source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                                      (SOURCE_ID,)).fetchone()
            if not source_state or source_state[0] != "closed":
                raise ValueError((source_state[1] if source_state and source_state[1]
                                  else "BioSpace sitemap source is not closed"))
            total, candidates = parse_month_sitemap(_read_saved(saved), month)
            observed = current.isoformat(timespec="seconds")
            for item in candidates:
                store_candidate_hint(db, source, item)
                db.execute("""INSERT INTO biospace_detail_jobs
                    (url,title,sitemap_month,document_type,mode,first_seen_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,sitemap_month=excluded.sitemap_month,
                    document_type=excluded.document_type""",
                    (item.url, item.title, month, item.document_type, mode, observed))
            db.execute("""UPDATE biospace_sitemap_jobs SET status='done',attempts=?,
                total_urls=?,exact_candidates=?,last_error=NULL,processed_at=?
                WHERE sitemap_month=?""",
                (attempts + 1, total, len(candidates), observed, month))
            _finish_state_if_complete(db, observed)
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "biospace_sitemap_month_success",
                   "sitemap_month": month, "total_urls": total,
                   "exact_candidates": len(candidates), "mode": mode,
                   "reused_saved_response": reused_saved})
        except (ValueError, OSError) as exc:
            source_state = db.execute("SELECT state FROM source_state WHERE source_id=?",
                                      (SOURCE_ID,)).fetchone()
            status = ("manual_review" if source_state and source_state[0] == "manual_review"
                      else "manual_review" if attempts + 1 >= 3 else "pending")
            db.execute("""UPDATE biospace_sitemap_jobs SET status=?,attempts=?,last_error=?
                WHERE sitemap_month=?""", (status, attempts + 1, str(exc)[:300], month))
            db.execute("UPDATE biospace_sitemap_state SET last_error=? WHERE source_id=?",
                       (str(exc)[:300], SOURCE_ID))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "biospace_sitemap_month_error",
                   "sitemap_month": month, "error": str(exc)[:300]})
        db.close()
        return 1

    if index_fetch_due(db, current):
        store_search_hint(db, INDEX_URL, "BioSpace sitemap index")
        db.commit()
        collect_source(db, source, user_agent, 1, False, INDEX_URL)
        try:
            ingest_saved_index(db, source, current)
        except ValueError as exc:
            db.execute("UPDATE biospace_sitemap_state SET last_error=? WHERE source_id=?",
                       (str(exc)[:300], SOURCE_ID))
            db.commit()
            audit({"source_id": SOURCE_ID, "outcome": "biospace_sitemap_index_error",
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
