#!/usr/bin/env python3
"""Low-volume adapter for the CSRC's public site-search form."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin, urlparse

from collector.collect import (
    ROOT, audit, canonicalize, collect_source, connect, enqueue_frontier, finish_frontier,
    load_sources,
)

SOURCE_ID = "csrc"
ENDPOINT = "https://www.csrc.gov.cn/guestweb4/s"
TERMS = {
    "cxmt": "长鑫科技集团股份有限公司",
    "wuxi_apptec": "无锡药明康德新药开发股份有限公司",
}


def ensure_jobs(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS csrc_search_jobs (
        company_id TEXT PRIMARY KEY, search_term TEXT NOT NULL,
        last_run_at TEXT, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)""")
    for company_id, term in TERMS.items():
        db.execute("""INSERT INTO csrc_search_jobs(company_id,search_term)
            VALUES(?,?) ON CONFLICT(company_id) DO UPDATE SET search_term=excluded.search_term""",
                   (company_id, term))
    db.commit()


def result_links(body: bytes, base_url: str = ENDPOINT) -> list[tuple[str, str | None, str | None]]:
    """Extract only first-class search-result anchors, excluding footer/navigation links."""
    text = body.decode("utf-8", errors="ignore")
    results: list[tuple[str, str | None, str | None]] = []
    for match in re.finditer(r"<a\b([^>]*)>(.*?)</a>", text, re.I | re.S):
        attrs, inner = match.groups()
        class_match = re.search(r'class=["\']([^"\']*)["\']', attrs, re.I)
        if not class_match or "titleFont" not in class_match.group(1).split():
            continue
        href_match = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I | re.S)
        if not href_match:
            continue
        url = canonicalize(urljoin(base_url, html.unescape(href_match.group(1)).strip()))
        if urlparse(url).hostname != "www.csrc.gov.cn" or not urlparse(url).path.endswith("/content.shtml"):
            continue
        title_match = re.search(r'title=["\']([^"\']+)["\']', attrs, re.I | re.S)
        title = html.unescape(title_match.group(1)).strip() if title_match else None
        if not title:
            title = re.sub(r"<[^>]+>", "", inner)
            title = html.unescape(re.sub(r"\s+", " ", title)).strip() or None
        following = text[match.end():match.end() + 4000]
        date_match = re.search(
            r'<span\b[^>]*class=["\'][^"\']*sourceDateFont[^"\']*["\'][^>]*>'
            r'\s*([12]\d{3}-\d{2}-\d{2})\s*</span>', following, re.I | re.S)
        item = (url, title, date_match.group(1) if date_match else None)
        if item not in results:
            results.append(item)
    return results


def store_hint(db, config: dict, company_id: str, url: str, title: str | None,
               published_at: str | None) -> None:
    legal_name = config["entities"][company_id]["canonical_name"]
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=COALESCE(excluded.title,discovered_metadata.title),
        published_at=COALESCE(excluded.published_at,discovered_metadata.published_at),
        company_id=excluded.company_id,matched_legal_entity=excluded.matched_legal_entity,
        original_timezone=excluded.original_timezone,review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, published_at, company_id, legal_name,
         "Asia/Shanghai", "candidate_entity_match"))


def fetch_one_candidate(db, source, user_agent: str) -> int:
    row = db.execute("""SELECT url FROM crawl_frontier
        WHERE source_id=? AND status='pending' AND discovered_from LIKE 'csrc_search:%'
        ORDER BY discovered_at,url LIMIT 1""", (SOURCE_ID,)).fetchone()
    if not row:
        return 0
    url = row[0]
    collect_source(db, source, user_agent, 1, False, url)
    saved = db.execute("SELECT 1 FROM documents WHERE source_id=? AND canonical_url=?",
                       (SOURCE_ID, canonicalize(url))).fetchone()
    state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                       (SOURCE_ID,)).fetchone()
    if saved and state and state[0] == "closed":
        finish_frontier(db, SOURCE_ID, url, "done")
    elif state and state[0] == "manual_review":
        finish_frontier(db, SOURCE_ID, url, "manual_review", state[1])
    return 1


def process_if_due(contact: str, now: datetime | None = None) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_jobs(db)
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    if fetch_one_candidate(db, source, user_agent):
        db.close()
        return 1

    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(days=1)).isoformat(timespec="seconds")
    job = db.execute("""SELECT company_id,search_term,attempts FROM csrc_search_jobs
        WHERE last_run_at IS NULL OR last_run_at<=?
        ORDER BY COALESCE(last_run_at,''),company_id LIMIT 1""", (cutoff,)).fetchone()
    if not job:
        db.close()
        return 0
    company_id, term, attempts = job
    url = ENDPOINT + "?" + urlencode({
        "searchWord": term, "uc": "1", "siteCode": "bm56000001", "column": "全部",
    })
    try:
        collect_source(db, source, user_agent, 1, False, url)
        row = db.execute("SELECT raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                         (SOURCE_ID, canonicalize(url))).fetchone()
        if not row:
            raise ValueError("CSRC search response was not saved")
        body = (ROOT / row[0]).read_bytes()
        links = result_links(body, url)
        skipped_old = 0
        for candidate, title, published_at in links:
            store_hint(db, config, company_id, candidate, title, published_at)
            enqueue_frontier(db, SOURCE_ID, candidate, f"csrc_search:{company_id}", 0)
            if published_at and published_at < str(source.raw["backfill_start"]):
                finish_frontier(
                    db, SOURCE_ID, candidate, "skipped_scope",
                    "official search result predates backfill boundary")
                skipped_old += 1
        db.execute("""UPDATE csrc_search_jobs SET last_run_at=?,status='done',attempts=?,last_error=NULL
            WHERE company_id=?""", (current.isoformat(timespec="seconds"), attempts + 1, company_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "official_search_success",
               "company_id": company_id, "search_term": term,
               "results_discovered": len(links), "results_skipped_before_boundary": skipped_old})
    except (ValueError, OSError) as exc:
        db.execute("""UPDATE csrc_search_jobs SET status='error',attempts=?,last_error=?
            WHERE company_id=?""", (attempts + 1, str(exc)[:300], company_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "official_search_error",
               "company_id": company_id, "error": str(exc)[:300]})
    finally:
        db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    process_if_due(contact)
