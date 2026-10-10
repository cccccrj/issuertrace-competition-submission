#!/usr/bin/env python3
"""Give every stored document a title a reviewer can recognise (I-05).

Two defects left sources on a risk dossier that nobody could identify:

* **Truncated at an apostrophe.**  The og:title pattern stopped at either quote
  character, so 14 Tom's Hardware articles — 「China's banned memory-maker…」 —
  were stored as "China".  The collectors are fixed; this re-reads the raw page
  for the ones already stored.
* **A listing label, not a title.**  cninfo lists every Hong Kong filing as
  「H股公告」 and a few IPO files arrive with no title at all.  The real title is
  on the first page of the document: the form name of an HKEX return
  (翌日披露報表), the heading under 股份代號 in an announcement, the cover lines
  of a prospectus.  It is read from there and shown beside the listing label —
  「H股公告｜翌日披露報表」 — so the label the publisher gave is not lost and a
  reader can see which part was read from the document.

The title as first stored is kept in ``source_title``.  Deterministic, no model
calls, idempotent: a second run changes 0 rows.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from collector.collect import ROOT, connect, meta_content
from processor.pipeline import add_column, text_file_path

#: Listing labels that name a kind of filing rather than the filing.
GENERIC_TITLES = frozenset({"", "H股公告"})
SEPARATOR = "｜"
#: Lines that open the body of an announcement rather than continue its heading.
BODY_START = re.compile(r"^(茲|兹|謹此|谨此|本公告|董事會（|董事会（)|「本公司」|（「本公司|\(「本公司")
#: The roster of banks printed under a placing's heading is not part of it.
ROLE_LINE = re.compile(r"協調人|协调人|賬簿管理人|账簿管理人|配售代理|保薦人|保荐人")
PAGE_MARK = re.compile(r"^\[PAGE \d+\]$|^[–-]\s*\d+\s*[–-]$")
TITLE_CAP = 80


def ensure_schema(db: sqlite3.Connection) -> None:
    add_column(db, "document_versions", "source_title", "TEXT")
    add_column(db, "document_versions", "title_basis", "TEXT")


def _lines(text: str, limit: int = 120) -> list[str]:
    found = []
    for line in text.splitlines()[:limit]:
        line = line.strip()
        if line and not PAGE_MARK.match(line):
            found.append(line)
    return found


CJK = r"\u3000-\u303f\u3400-\u9fff\uff00-\uffef"


def _tidy(title: str) -> str:
    # PDF extraction breaks Chinese headings into spaced fragments; spaces next
    # to a Chinese character are layout, spaces between Latin words are not.
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(rf"(?<=[{CJK}])\s+|\s+(?=[{CJK}])", "", title)
    # 「股份發行人及根據《上市規則》…發行人的證券變動月報表」 is the form's legal
    # name; what distinguishes it from the other HKEX returns is the last clause.
    if len(title) > 20 and title.endswith(("報表", "报表")) and "的" in title:
        title = title.rsplit("的", 1)[1]
    return title[:TITLE_CAP]


def title_from_text(text: str) -> str | None:
    """The heading a filing prints on its first page, or None when there is none to read."""
    lines = _lines(text)
    # An HKEX return: a form code (FF301, FF304…) and then its name.
    for index, line in enumerate(lines[:12]):
        if re.match(r"^FF\d{3}", line):
            for candidate in lines[index + 1:index + 8]:
                if re.search(r"報表|报表", candidate):
                    return _tidy(candidate)
    # An announcement: the heading sits under the stock-code line.
    for index, line in enumerate(lines[:80]):
        if re.search(r"股份代[號号]|stock code", line, re.I):
            parts = []
            for candidate in lines[index + 1:index + 5]:
                numbered = re.match(r"^[（(]\d+[）)]", candidate)
                if (BODY_START.search(candidate) or ROLE_LINE.search(candidate) or len(candidate) > 45
                        or (candidate.startswith(("（", "(")) and not numbered)):
                    break
                parts.append(candidate)
            return _tidy("".join(parts)) if parts else None
    # A prospectus or reply: the cover lines before the sponsor's name.  Only a
    # Chinese cover reads this way; an English web page's first lines are a
    # navigation banner ("Here's how you know…").
    if not re.search(rf"[{CJK}]", "".join(lines[:10])):
        return None
    parts = []
    for candidate in lines[:10]:
        if re.search(r"保荐机构|主承销商|声明|风险提示", candidate):
            break
        if (re.fullmatch(r"[A-Za-z .,&]+", candidate) or re.search(r"\d+\s*号|大街|大厦", candidate)
                or "http" in candidate):
            continue
        if candidate.startswith(("（", "(")) and re.search(r"号|路|区|市", candidate):
            continue
        parts.append(candidate)
    title = "".join(parts)
    return _tidy(title) if 6 <= len(title) <= 90 else None


def reparsed_title(stored: str, raw_path: str | None) -> str | None:
    """The full og:title when the stored one is it cut off at a quote character."""
    if not stored or not raw_path or not raw_path.lower().endswith((".html", ".htm")):
        return None
    path = Path(raw_path) if Path(raw_path).is_absolute() else ROOT / raw_path
    if not path.exists():
        return None
    full = meta_content(path.read_bytes()[:600_000].decode("utf-8", errors="ignore"), "og:title")
    if (full and len(full) > len(stored) and full.startswith(stored)
            and full[len(stored):].lstrip()[:1] in ("'", '"', "’", "‘", "“", "”")):
        return full
    return None


def repair(db: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, Any]:
    # A dry run writes nothing, the two columns included.
    if not dry_run:
        ensure_schema(db)
    columns = {row[1] for row in db.execute("PRAGMA table_info(document_versions)")}
    original = "source_title" if "source_title" in columns else "NULL"
    rows = db.execute(f"""SELECT version_id, title, raw_path, content_hash, {original}
        FROM document_versions""").fetchall()
    changes: list[tuple[str, str, str, str | None]] = []
    unresolved = 0
    for version_id, title, raw_path, content_hash, source_title in rows:
        stored = (title or "").strip()
        fixed = reparsed_title(stored, raw_path)
        if fixed:
            changes.append((version_id, fixed, "og_title_reparsed", source_title or title))
            continue
        if stored not in GENERIC_TITLES:
            continue
        path = text_file_path(content_hash) if content_hash else None
        derived = title_from_text(path.read_text(encoding="utf-8")) if path and path.exists() else None
        if not derived:
            unresolved += 1
            continue
        display = f"{stored}{SEPARATOR}{derived}" if stored else derived
        changes.append((version_id, display, "read_from_document", source_title or title))
    if not dry_run:
        db.executemany("""UPDATE document_versions SET title=?, title_basis=?,
            source_title=COALESCE(source_title, ?) WHERE version_id=?""",
                       [(title, basis, original, version_id) for version_id, title, basis, original in changes])
        db.commit()
    by_basis: dict[str, int] = {}
    for _, _, basis, _ in changes:
        by_basis[basis] = by_basis.get(basis, 0) + 1
    return {"changed": len(changes), "by_basis": by_basis, "unresolved_generic": unresolved,
            "dry_run": dry_run}


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair truncated and generic document titles")
    parser.add_argument("--dry-run", action="store_true", help="count what would change, write nothing")
    args = parser.parse_args()
    db = connect()
    print(json.dumps(repair(db, dry_run=args.dry_run), ensure_ascii=False, indent=2))
    db.close()


if __name__ == "__main__":
    main()
