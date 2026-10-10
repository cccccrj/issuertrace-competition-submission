#!/usr/bin/env python3
"""Low-volume use of MIIT's same-origin public search interface."""
from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

from collector.collect import (
    ROOT, audit, blocked_host, canonicalize, collect_source, connect, enqueue_frontier,
    load_sources,
)

SOURCE_ID = "miit"
ENDPOINT = "https://www.miit.gov.cn/search-front-server/api/search/info"
TERMS = {
    "cxmt_legal": "长鑫科技集团股份有限公司",
    "cxmt_storage": "长鑫存储",
    "cxmt_english": "CXMT",
}
PAGE_SIZE = 10


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS miit_search_state (
        term_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, search_term TEXT NOT NULL,
        last_completed_at TEXT, total_results INTEGER, last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS miit_search_jobs (
        job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, term_id TEXT NOT NULL,
        page_no INTEGER NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT)""")
    for term_id, term in TERMS.items():
        db.execute("""INSERT INTO miit_search_state(term_id,company_id,search_term)
            VALUES(?,'cxmt',?) ON CONFLICT(term_id) DO UPDATE SET search_term=excluded.search_term""",
                   (term_id, term))
    db.commit()


def ensure_due_runs(db, now: datetime) -> None:
    cutoff = now - timedelta(days=7)
    for term_id, last_completed in db.execute(
            "SELECT term_id,last_completed_at FROM miit_search_state ORDER BY term_id").fetchall():
        if db.execute("""SELECT 1 FROM miit_search_jobs WHERE term_id=?
                AND status!='done' LIMIT 1""", (term_id,)).fetchone():
            continue
        if last_completed and datetime.fromisoformat(last_completed) > cutoff:
            continue
        mode = "backfill" if not last_completed else "incremental"
        run_id = f"{mode}:{term_id}:{now.date().isoformat()}"
        db.execute("""INSERT OR IGNORE INTO miit_search_jobs
            (job_id,run_id,term_id,page_no,mode) VALUES(?,?,?,1,?)""",
                   (f"{run_id}:1", run_id, term_id, mode))
    db.commit()


def suspend_pending_jobs(db, reason: str) -> int:
    changed = db.execute("""UPDATE miit_search_jobs SET status='manual_review',last_error=?
        WHERE status='pending'""", (reason,)).rowcount
    db.execute("""UPDATE miit_search_state SET last_error=? WHERE term_id IN
        (SELECT term_id FROM miit_search_jobs WHERE status='manual_review')""", (reason,))
    db.commit()
    return changed


def search_url(term: str, page_no: int, start: str, end: str) -> str:
    return ENDPOINT + "?" + urlencode({
        "websiteid": "110000000000000", "scope": "basic", "q": term,
        "pg": PAGE_SIZE, "cateid": 54, "pos": "title_text,infocontent",
        "pq": "", "oq": "", "eq": "", "begin": start, "end": end,
        "dateField": "deploytime",
        "selectFields": "title,title_text,deploytime,url,infocontent,webid,columnname",
        "group": "distinct", "highlightFields": "title_text,infocontent,webid",
        "level": 6,
        "sortFields": '[{"name":"deploytime","type":"desc"}]', "p": page_no,
    })


def plain(value: object) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()


def published_date(value: object) -> str | None:
    try:
        stamp = int(str(value)) / 1000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).astimezone(
        ZoneInfo("Asia/Shanghai")).date().isoformat()


def result_items(payload: dict, exact_term: str) -> tuple[list[tuple[str, str, str | None, str]], int]:
    if not payload.get("success") or str(payload.get("code")) != "200":
        raise ValueError(str(payload.get("message") or "MIIT search returned unsuccessful status"))
    search_result = (payload.get("data") or {}).get("searchResult") or {}
    total = int(search_result.get("total") or 0)
    results: list[tuple[str, str, str | None, str]] = []
    for group in search_result.get("dataResults") or []:
        entries = group.get("groupData") or []
        if not entries:
            continue
        data = entries[0].get("data") or {}
        title = plain(data.get("title_text") or data.get("title"))
        content = plain(data.get("infocontent"))
        if exact_term.lower() not in (title + "\n" + content).lower():
            continue
        candidate = urljoin("https://www.miit.gov.cn/", plain(data.get("url")))
        parsed = urlparse(candidate)
        if parsed.hostname != "www.miit.gov.cn" or not parsed.path.endswith(".html"):
            continue
        lowered = title.lower()
        if "标准" in title:
            document_type = "industry_standard"
        elif "名单" in title or "目录" in title:
            document_type = "industry_list"
        elif "批复" in title or "许可" in title:
            document_type = "administrative_approval"
        else:
            document_type = "industry_policy"
        item = (canonicalize(candidate), title, published_date(data.get("deploytime")), document_type)
        if item not in results:
            results.append(item)
    return results, total


def store_hint(db, config: dict, url: str, title: str | None, published_at: str | None,
               document_type: str, matched_entity: str,
               review_status: str = "candidate_entity_match") -> None:
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,published_at=excluded.published_at,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, published_at, "cxmt", matched_entity,
         document_type, "Asia/Shanghai", review_status))


def fetch_one_candidate(db, source, user_agent: str) -> int:
    row = db.execute("""SELECT url FROM crawl_frontier WHERE source_id=? AND status='pending'
        ORDER BY depth,priority,discovered_at,url LIMIT 1""", (SOURCE_ID,)).fetchone()
    if not row:
        return 0
    collect_source(db, source, user_agent, 1, True, row[0])
    return 1


def process_one(contact: str, now: datetime | None = None) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    source_state = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?",
                              (SOURCE_ID,)).fetchone()
    host_reason = blocked_host(db, SOURCE_ID, "www.miit.gov.cn")
    if host_reason or (source_state and source_state[0] == "manual_review"):
        reason = host_reason or source_state[1] or "source requires manual review"
        changed = suspend_pending_jobs(db, reason)
        if changed:
            audit({"source_id": SOURCE_ID, "outcome": "miit_search_suspended_manual_review",
                   "error": reason, "jobs_suspended": changed})
        db.close()
        return 0
    current = now or datetime.now(timezone.utc)
    ensure_due_runs(db, current)
    job = db.execute("""SELECT j.job_id,j.run_id,j.term_id,j.page_no,j.mode,j.attempts,
        s.search_term FROM miit_search_jobs j JOIN miit_search_state s ON s.term_id=j.term_id
        WHERE j.status='pending' ORDER BY CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,
        j.term_id,j.page_no LIMIT 1""").fetchone()
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    if not job:
        fetched = fetch_one_candidate(db, source, user_agent)
        db.close()
        return fetched

    job_id, run_id, term_id, page_no, mode, attempts, term = job
    start = config["scope"]["backfill_start"]
    end = current.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    url = search_url(term, page_no, start, end)
    store_hint(db, config, url, f"工信部精确检索：{term}", None, "search_response", term,
               "search_response")
    db.commit()
    collect_source(db, source, user_agent, 1, False, url)
    row = db.execute("SELECT raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                     (SOURCE_ID, canonicalize(url))).fetchone()
    try:
        if not row:
            raise ValueError("MIIT search response was not saved")
        payload = json.loads((ROOT / row[0]).read_text(encoding="utf-8"))
        results, total = result_items(payload, term)
        pages = max(math.ceil(total / PAGE_SIZE), 1)
        if pages > 50:
            raise ValueError(f"exact search returned implausible page count {pages}")
        for candidate, title, published_at, document_type in results:
            store_hint(db, config, candidate, title, published_at, document_type, term)
            enqueue_frontier(db, SOURCE_ID, candidate, f"miit_search:{term_id}", 0)
        if page_no == 1:
            for extra_page in range(2, pages + 1):
                db.execute("""INSERT OR IGNORE INTO miit_search_jobs
                    (job_id,run_id,term_id,page_no,mode) VALUES(?,?,?,?,?)""",
                           (f"{run_id}:{extra_page}", run_id, term_id, extra_page, mode))
        processed_at = current.isoformat(timespec="seconds")
        db.execute("""UPDATE miit_search_jobs SET status='done',attempts=?,last_error=NULL,
            processed_at=? WHERE job_id=?""", (attempts + 1, processed_at, job_id))
        remaining = db.execute("""SELECT 1 FROM miit_search_jobs
            WHERE run_id=? AND status!='done' LIMIT 1""", (run_id,)).fetchone()
        if not remaining:
            db.execute("""UPDATE miit_search_state SET last_completed_at=?,total_results=?,
                last_error=NULL WHERE term_id=?""", (processed_at, total, term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "miit_search_success",
               "job_id": job_id, "term_id": term_id, "search_term": term,
               "page": page_no, "page_count": pages, "total_results": total,
               "results_discovered": len(results)})
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        status = "manual_review" if attempts + 1 >= 3 else "pending"
        db.execute("UPDATE miit_search_jobs SET status=?,attempts=?,last_error=? WHERE job_id=?",
                   (status, attempts + 1, str(exc)[:300], job_id))
        db.execute("UPDATE miit_search_state SET last_error=? WHERE term_id=?",
                   (str(exc)[:300], term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "miit_search_error",
               "job_id": job_id, "term_id": term_id, "error": str(exc)[:300]})
    finally:
        db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    process_one(contact)
