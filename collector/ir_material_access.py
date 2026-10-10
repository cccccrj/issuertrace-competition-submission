#!/usr/bin/env python3
"""Index visible WuXi IR material metadata when downloads require identity review."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from collector.collect import ROOT, audit, connect

SOURCE_ID = "wuxi_ir_materials"
ACCESS_MARKER = "Please fill in the real information for review"
ACCESS_REASON = (
    "official IR page exposes the material title/date but no public download URL and "
    "asks for real information for review; no identity submission or access bypass attempted"
)


@dataclass(frozen=True)
class MaterialLink:
    published_at: str
    title: str
    material_label: str
    href: str | None


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    value = next((value for key, value in attrs if key == "class"), None) or ""
    return set(value.split())


class _MaterialParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.card_depth = 0
        self.card: dict[str, object] | None = None
        self.capture: str | None = None
        self.capture_depth = 0
        self.buffer: list[str] = []
        self.cards: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = _classes(attrs)
        if tag == "div" and "list-item" in classes and self.card is None:
            self.card = {"date": "", "title": "", "links": []}
            self.card_depth = 1
            return
        if self.card is None:
            return
        if tag == "div":
            self.card_depth += 1
        if self.capture:
            if tag not in self.VOID_TAGS:
                self.capture_depth += 1
            return
        if tag == "p" and "date" in classes:
            self.capture, self.capture_depth, self.buffer = "date", 1, []
        elif tag == "h1" and "title" in classes:
            self.capture, self.capture_depth, self.buffer = "title", 1, []
        elif tag == "a":
            href = next((value for key, value in attrs if key == "href"), None)
            self.capture, self.capture_depth, self.buffer = "link", 1, []
            self.card["current_href"] = href

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.capture:
            return

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.card is None:
            return
        if self.capture:
            self.capture_depth -= 1
            if self.capture_depth == 0:
                value = re.sub(r"\s+", " ", html.unescape("".join(self.buffer))).strip()
                if self.capture == "link":
                    if value:
                        links = self.card["links"]
                        assert isinstance(links, list)
                        links.append((value, self.card.pop("current_href", None)))
                else:
                    self.card[self.capture] = value
                self.capture = None
                self.buffer = []
            return
        if tag == "div":
            self.card_depth -= 1
            if self.card_depth == 0:
                self.cards.append(self.card)
                self.card = None


def parse_material_links(body: bytes) -> list[MaterialLink]:
    text = body.decode("utf-8", errors="ignore")
    if ACCESS_MARKER.casefold() not in text.casefold():
        return []
    parser = _MaterialParser()
    parser.feed(text)
    results: list[MaterialLink] = []
    for card in parser.cards:
        date_match = re.fullmatch(r"([12]\d{3})/(\d{1,2})/(\d{1,2})", str(card["date"]))
        title = str(card["title"]).strip()
        if not date_match or not title:
            continue
        published_at = f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
        for label, href in card["links"]:  # type: ignore[assignment]
            clean_href = str(href).strip() if href else None
            results.append(MaterialLink(published_at, title, str(label), clean_href))
    return results


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS ir_material_access_jobs (
        job_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, published_at TEXT NOT NULL,
        title TEXT NOT NULL, material_label TEXT NOT NULL, status TEXT NOT NULL,
        public_url TEXT, reason TEXT NOT NULL, first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(ir_material_access_jobs)")}
    if "public_url" not in columns:
        db.execute("ALTER TABLE ir_material_access_jobs ADD COLUMN public_url TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS ir_material_scan_state (
        source_url TEXT PRIMARY KEY, content_hash TEXT NOT NULL, scanned_at TEXT NOT NULL,
        visible_links INTEGER NOT NULL, gated_links INTEGER NOT NULL, last_error TEXT)""")
    db.commit()


def store_gated_jobs(db, source_url: str, materials: list[MaterialLink],
                     backfill_start: str, observed_at: str) -> list[MaterialLink]:
    created: list[MaterialLink] = []
    for item in materials:
        if item.published_at < backfill_start:
            continue
        job_id = hashlib.sha256(
            f"{item.published_at}\0{item.title}\0{item.material_label}".encode()).hexdigest()
        exists = db.execute(
            "SELECT 1 FROM ir_material_access_jobs WHERE job_id=?", (job_id,)).fetchone()
        if item.href:
            if exists:
                db.execute("""UPDATE ir_material_access_jobs SET source_url=?,status='public_url_available',
                    public_url=?,reason='public download URL is now visible; normal attachment collection applies',
                    last_seen_at=? WHERE job_id=?""",
                    (source_url, item.href, observed_at, job_id))
            continue
        db.execute("""INSERT INTO ir_material_access_jobs
            (job_id,source_url,published_at,title,material_label,status,public_url,reason,
             first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,'manual_review',NULL,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
            source_url=excluded.source_url,status='manual_review',public_url=NULL,
            last_seen_at=excluded.last_seen_at,reason=excluded.reason""",
            (job_id, source_url, item.published_at, item.title, item.material_label,
             ACCESS_REASON, observed_at, observed_at))
        if not exists:
            created.append(item)
    return created


def scan_saved_pages(root: Path = ROOT, backfill_start: str = "2024-01-01") -> int:
    db = connect()
    ensure_schema(db)
    rows = db.execute("""SELECT canonical_url,content_hash,raw_path FROM documents
        WHERE source_id=? AND status=200 ORDER BY canonical_url""", (SOURCE_ID,)).fetchall()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scanned = 0
    new_jobs: list[tuple[str, MaterialLink]] = []
    for source_url, content_hash, raw_path in rows:
        prior = db.execute("SELECT content_hash FROM ir_material_scan_state WHERE source_url=?",
                           (source_url,)).fetchone()
        if prior and prior[0] == content_hash:
            continue
        path = root / raw_path
        if not path.exists():
            db.execute("""INSERT INTO ir_material_scan_state
                (source_url,content_hash,scanned_at,visible_links,gated_links,last_error)
                VALUES(?,?,?,0,0,?) ON CONFLICT(source_url) DO UPDATE SET
                content_hash=excluded.content_hash,scanned_at=excluded.scanned_at,
                last_error=excluded.last_error""",
                (source_url, content_hash, now, "raw file missing"))
            continue
        materials = parse_material_links(path.read_bytes())
        created = store_gated_jobs(db, source_url, materials, backfill_start, now)
        new_jobs.extend((source_url, item) for item in created)
        gated = sum(1 for item in materials if not item.href and item.published_at >= backfill_start)
        visible = sum(1 for item in materials if item.href and item.published_at >= backfill_start)
        db.execute("""INSERT INTO ir_material_scan_state
            (source_url,content_hash,scanned_at,visible_links,gated_links,last_error)
            VALUES(?,?,?,?,?,NULL) ON CONFLICT(source_url) DO UPDATE SET
            content_hash=excluded.content_hash,scanned_at=excluded.scanned_at,
            visible_links=excluded.visible_links,gated_links=excluded.gated_links,last_error=NULL""",
            (source_url, content_hash, now, visible, gated))
        scanned += 1
    db.commit()
    db.close()
    for source_url, item in new_jobs:
        audit({"source_id": SOURCE_ID, "outcome": "ir_material_access_manual_review",
               "url": source_url, "company_id": "wuxi_apptec", "title": item.title,
               "published_at": item.published_at, "material_label": item.material_label,
               "error": ACCESS_REASON})
    if scanned:
        audit({"source_id": SOURCE_ID, "outcome": "ir_material_access_scan",
               "scanned_pages": scanned, "new_manual_jobs": len(new_jobs)})
    return len(new_jobs)


if __name__ == "__main__":
    print(f"new_manual_jobs={scan_saved_pages()}")
