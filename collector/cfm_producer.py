#!/usr/bin/env python3
"""Low-volume collection from CFM's public CXMT producer page."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from collector.collect import (
    ROOT, audit, blocked_host, canonicalize, collect_source, connect, load_sources,
)

SOURCE_ID = "chinaflashmarket"


@dataclass(frozen=True)
class ProducerItem:
    url: str
    title: str
    published_at: str
    document_type: str


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    value = next((value for key, value in attrs if key == "class"), None) or ""
    return set(value.split())


class _ProducerParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.card: dict[str, object] | None = None
        self.card_depth = 0
        self.anchor_depth = 0
        self.anchor_href: str | None = None
        self.anchor_text: list[str] = []
        self.card_text: list[str] = []
        self.cards: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = _classes(attrs)
        if tag == "div" and self.card is None and ({"newsli", "flash-item"} & classes):
            self.card = {"kind": "newsflash" if "flash-item" in classes else "article",
                         "links": []}
            self.card_depth = 1
            return
        if self.card is None:
            return
        if tag == "div":
            self.card_depth += 1
        if self.anchor_depth:
            if tag not in self.VOID_TAGS:
                self.anchor_depth += 1
        elif tag == "a":
            self.anchor_depth = 1
            self.anchor_href = next((value for key, value in attrs if key == "href"), None)
            self.anchor_text = []

    def handle_data(self, data: str) -> None:
        if self.card is not None:
            self.card_text.append(data)
        if self.anchor_depth:
            self.anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.card is None:
            return
        if self.anchor_depth:
            self.anchor_depth -= 1
            if self.anchor_depth == 0:
                label = re.sub(r"\s+", " ", html.unescape("".join(self.anchor_text))).strip()
                if self.anchor_href and label:
                    links = self.card["links"]
                    assert isinstance(links, list)
                    links.append((urljoin(self.base_url, self.anchor_href), label))
                self.anchor_href = None
                self.anchor_text = []
            return
        if tag == "div":
            self.card_depth -= 1
            if self.card_depth == 0:
                self.card["text"] = re.sub(
                    r"\s+", " ", html.unescape("".join(self.card_text))).strip()
                self.cards.append(self.card)
                self.card = None
                self.card_text = []


def parse_producer_items(body: bytes, base_url: str) -> list[ProducerItem]:
    parser = _ProducerParser(base_url)
    parser.feed(body.decode("utf-8", errors="ignore"))
    items: list[ProducerItem] = []
    seen: set[str] = set()
    expected_host = urlparse(base_url).hostname
    for card in parser.cards:
        match = re.search(r"(?<!\d)([12]\d{3})-(\d{2})-(\d{2})(?!\d)",
                          str(card.get("text", "")))
        if not match:
            continue
        published_at = "-".join(match.groups())
        expected_prefix = "/newsflash/" if card["kind"] == "newsflash" else "/a/"
        for raw_url, title in card["links"]:  # type: ignore[assignment]
            url = canonicalize(str(raw_url))
            parsed = urlparse(url)
            if parsed.hostname != expected_host or not parsed.path.startswith(expected_prefix):
                continue
            if url not in seen:
                items.append(ProducerItem(
                    url, re.sub(r"\s+", " ", str(title)).strip(), published_at,
                    "market_newsflash" if card["kind"] == "newsflash" else "company_updates"))
                seen.add(url)
            break
    return items


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS cfm_producer_state (
        source_id TEXT PRIMARY KEY, seed_content_hash TEXT, last_seed_fetched_at TEXT,
        last_scanned_at TEXT, in_scope_items INTEGER NOT NULL DEFAULT 0,
        skipped_before_window INTEGER NOT NULL DEFAULT 0, last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS cfm_producer_jobs (
        url TEXT PRIMARY KEY, title TEXT NOT NULL, published_at TEXT NOT NULL,
        document_type TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, processed_at TEXT,
        first_seen_at TEXT NOT NULL)""")
    db.commit()


def refresh_from_saved(db, source) -> int:
    row = db.execute("""SELECT content_hash,raw_path,fetched_at FROM documents
        WHERE source_id=? AND canonical_url=?""",
        (SOURCE_ID, canonicalize(source.raw["base_url"]))).fetchone()
    if not row:
        return 0
    content_hash, raw_path, fetched_at = row
    state = db.execute("SELECT seed_content_hash FROM cfm_producer_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    if state and state[0] == content_hash:
        return 0
    path = ROOT / raw_path
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not path.exists():
        db.execute("""INSERT INTO cfm_producer_state
            (source_id,seed_content_hash,last_seed_fetched_at,last_scanned_at,last_error)
            VALUES(?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
            seed_content_hash=excluded.seed_content_hash,last_seed_fetched_at=excluded.last_seed_fetched_at,
            last_scanned_at=excluded.last_scanned_at,last_error=excluded.last_error""",
            (SOURCE_ID, content_hash, fetched_at, now, "raw seed file missing"))
        db.commit()
        return 0
    items = parse_producer_items(path.read_bytes(), source.raw["base_url"])
    start = str(source.raw["backfill_start"])
    initial = state is None
    inserted = 0
    for item in items:
        if item.published_at < start:
            continue
        exists = db.execute("SELECT 1 FROM cfm_producer_jobs WHERE url=?", (item.url,)).fetchone()
        db.execute("""INSERT INTO cfm_producer_jobs
            (url,title,published_at,document_type,mode,first_seen_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
            title=excluded.title,published_at=excluded.published_at,
            document_type=excluded.document_type""",
            (item.url, item.title, item.published_at, item.document_type,
             "backfill" if initial else "incremental", now))
        inserted += not bool(exists)
    db.execute("""INSERT INTO cfm_producer_state
        (source_id,seed_content_hash,last_seed_fetched_at,last_scanned_at,
         in_scope_items,skipped_before_window,last_error)
        VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(source_id) DO UPDATE SET
        seed_content_hash=excluded.seed_content_hash,last_seed_fetched_at=excluded.last_seed_fetched_at,
        last_scanned_at=excluded.last_scanned_at,in_scope_items=excluded.in_scope_items,
        skipped_before_window=excluded.skipped_before_window,last_error=NULL""",
        (SOURCE_ID, content_hash, fetched_at, now,
         sum(item.published_at >= start for item in items),
         sum(item.published_at < start for item in items)))
    db.commit()
    audit({"source_id": SOURCE_ID, "outcome": "cfm_producer_seed_scanned",
           "visible_items": len(items), "in_scope_items": sum(item.published_at >= start for item in items),
           "skipped_before_window": sum(item.published_at < start for item in items),
           "new_jobs": inserted})
    return inserted


def store_hint(db, source, item: ProducerItem) -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,published_at=excluded.published_at,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(item.url), item.title, item.published_at, "cxmt",
         source.raw.get("_matched_legal_entity"), item.document_type, "Asia/Shanghai",
         "candidate_entity_match"))


def suspend_pending(db, reason: str) -> int:
    changed = db.execute("""UPDATE cfm_producer_jobs SET status='manual_review',last_error=?
        WHERE status='pending'""", (reason,)).rowcount
    db.commit()
    return changed


def seed_fetch_due(db, now: datetime | None = None) -> bool:
    row = db.execute("SELECT last_seed_fetched_at FROM cfm_producer_state WHERE source_id=?",
                     (SOURCE_ID,)).fetchone()
    if not row or not row[0]:
        return True
    current = now or datetime.now(timezone.utc)
    return datetime.fromisoformat(row[0]) + timedelta(days=1) <= current


def process_one(contact: str) -> int:
    _, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    refresh_from_saved(db, source)
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    reason = blocked_host(db, SOURCE_ID, urlparse(source.raw["base_url"]).hostname)
    if reason or (state and state[0] == "manual_review"):
        reason = reason or state[1] or "source requires manual review"
        changed = suspend_pending(db, reason)
        if changed:
            audit({"source_id": SOURCE_ID, "outcome": "cfm_producer_suspended_manual_review",
                   "jobs_suspended": changed, "error": reason})
        db.close()
        return 0
    job = db.execute("""SELECT url,title,published_at,document_type,attempts
        FROM cfm_producer_jobs WHERE status='pending'
        ORDER BY published_at DESC,url LIMIT 1""").fetchone()
    if not job:
        if seed_fetch_due(db):
            user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
            collect_source(db, source, user_agent, 1, False, source.raw["base_url"])
            refresh_from_saved(db, source)
            db.close()
            return 1
        db.close()
        return 0
    url, title, published_at, document_type, attempts = job
    item = ProducerItem(url, title, published_at, document_type)
    store_hint(db, source, item)
    db.commit()
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    collect_source(db, source, user_agent, 1, False, url)
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    saved = db.execute("SELECT status,raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                       (SOURCE_ID, canonicalize(url))).fetchone()
    if saved and int(saved[0]) == 200 and saved[1] and source_state and source_state[0] == "closed":
        processed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("""UPDATE cfm_producer_jobs SET status='done',attempts=?,last_error=NULL,
            processed_at=? WHERE url=?""", (attempts + 1, processed, url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "cfm_producer_job_success",
               "url": url, "published_at": published_at, "document_type": document_type})
    else:
        reason = ((source_state[1] if source_state and source_state[1] else None) or
                  "CFM detail response was not saved")
        status = "manual_review" if (source_state and source_state[0] == "manual_review") or attempts + 1 >= 3 else "pending"
        db.execute("""UPDATE cfm_producer_jobs SET status=?,attempts=?,last_error=? WHERE url=?""",
                   (status, attempts + 1, reason[:300], url))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "cfm_producer_job_error",
               "url": url, "error": reason[:300]})
    db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    print(f"processed={process_one(contact)}")
