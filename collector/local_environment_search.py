#!/usr/bin/env python3
"""Exact-term collection from the Anhui ecology department's public site search."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from collector.collect import (
    ROOT, audit, canonicalize, collect_source, connect, enqueue_frontier, finish_frontier,
    load_sources,
)

SOURCE_ID = "local_environment"
ENDPOINT = "https://sthjt.ah.gov.cn/site/search/6788031"
TERMS = {
    "cxmt_legal": ("cxmt", "长鑫科技集团股份有限公司"),
    "cxmt_storage": ("cxmt", "长鑫存储技术有限公司"),
    "cxmt_xinqiao": ("cxmt", "长鑫新桥存储技术有限公司"),
    "wuxi_legal": ("wuxi_apptec", "无锡药明康德新药开发股份有限公司"),
}
TYPE_CODE = (
    "articleNews,pictureNews,videoNews,policyDoc,explainDoc,messageBoard,"
    "interviewInfo,collectInfo,survey,public_content,ordinaryPage"
)


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS local_environment_search_state (
        term_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, search_term TEXT NOT NULL,
        last_completed_at TEXT, last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS local_environment_search_jobs (
        job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, term_id TEXT NOT NULL,
        page_no INTEGER NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT)""")
    for term_id, (company_id, term) in TERMS.items():
        db.execute("""INSERT INTO local_environment_search_state(term_id,company_id,search_term)
            VALUES(?,?,?) ON CONFLICT(term_id) DO UPDATE SET
            company_id=excluded.company_id,search_term=excluded.search_term""",
                   (term_id, company_id, term))
    db.commit()


def ensure_due_runs(db, now: datetime) -> None:
    cutoff = now - timedelta(days=7)
    for term_id, last_completed in db.execute("""SELECT term_id,last_completed_at
            FROM local_environment_search_state ORDER BY term_id""").fetchall():
        unfinished = db.execute("""SELECT 1 FROM local_environment_search_jobs
            WHERE term_id=? AND status!='done' LIMIT 1""", (term_id,)).fetchone()
        if unfinished:
            continue
        if last_completed and datetime.fromisoformat(last_completed) > cutoff:
            continue
        mode = "backfill" if not last_completed else "incremental"
        run_id = f"{mode}:{term_id}:{now.date().isoformat()}"
        db.execute("""INSERT OR IGNORE INTO local_environment_search_jobs
            (job_id,run_id,term_id,page_no,mode) VALUES(?,?,?,1,?)""",
                   (f"{run_id}:1", run_id, term_id, mode))
    db.commit()


def result_items(body: bytes, exact_term: str) -> list[tuple[str, str, str | None, str]]:
    """Return URL, title, date and document type for exact-term result cards."""
    text = body.decode("utf-8", errors="replace")
    results: list[tuple[str, str, str | None, str]] = []
    for match in re.finditer(
            r'<ul\b[^>]*class=["\'][^"\']*\bsearch-list\b[^"\']*\bsearch-gk\b[^"\']*'
            r'["\'][^>]*>(.*?)</ul>', text, re.I | re.S):
        block = match.group(1)
        anchor = re.search(r'<a\b([^>]*)>(.*?)</a>', block, re.I | re.S)
        if not anchor:
            continue
        attrs, inner = anchor.groups()
        href = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if not href:
            continue
        url = canonicalize(html.unescape(href.group(1)).strip())
        parsed = urlparse(url)
        if parsed.hostname != "sthjt.ah.gov.cn" or not parsed.path.endswith(".html"):
            continue
        title_attr = re.search(r'title=["\']([^"\']+)["\']', attrs, re.I | re.S)
        title = html.unescape(title_attr.group(1)).strip() if title_attr else ""
        if not title:
            title = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        plain_block = html.unescape(re.sub(r"<[^>]+>", "", block))
        if exact_term not in title and exact_term not in plain_block:
            continue
        date_match = re.search(r"发文日期\s*[：:]\s*([12]\d{3}-\d{2}-\d{2})", plain_block)
        lowered = title.lower()
        if "危险废物" in title or "许可" in title:
            document_type = "environmental_permission"
        elif "环评" in title or "环境影响" in title:
            document_type = "environmental_impact_assessment"
        elif "验收" in title:
            document_type = "environmental_acceptance"
        elif "处罚" in title:
            document_type = "environmental_penalty"
        else:
            document_type = "environmental_record"
        item = (url, title, date_match.group(1) if date_match else None, document_type)
        if item not in results:
            results.append(item)
    return results


def page_count(body: bytes) -> int:
    text = body.decode("utf-8", errors="ignore")
    match = re.search(r"\bpageCount\s*:\s*(\d+)", text)
    return max(int(match.group(1)), 1) if match else 1


def search_url(term: str, page_no: int) -> str:
    return ENDPOINT + "?" + urlencode({
        "isAllSite": "false", "platformCode": "anhui_szbm_5", "siteId": "6788031",
        "typeCode": TYPE_CODE, "keywords": term, "sort": "intelligent", "orderType": "0",
        "fuzzySearch": "false", "pageIndex": page_no, "pageSize": 10,
    })


def store_hint(db, config: dict, company_id: str, url: str, title: str | None,
               published_at: str | None, document_type: str,
               matched_legal_entity: str | None = None) -> None:
    legal_name = matched_legal_entity or config["entities"][company_id]["canonical_name"]
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,published_at=excluded.published_at,
        company_id=excluded.company_id,matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, published_at, company_id, legal_name,
         document_type, "Asia/Shanghai", "candidate_entity_match"))


def fetch_one_candidate(db, source, user_agent: str) -> int:
    row = db.execute("""SELECT url FROM crawl_frontier
        WHERE source_id=? AND status='pending'
        ORDER BY depth,priority,discovered_at,url LIMIT 1""", (SOURCE_ID,)).fetchone()
    if not row:
        return 0
    url = row[0]
    collect_source(db, source, user_agent, 1, True, url)
    return 1


def process_one(contact: str, now: datetime | None = None) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    current = now or datetime.now(timezone.utc)
    ensure_due_runs(db, current)
    job = db.execute("""SELECT j.job_id,j.run_id,j.term_id,j.page_no,j.mode,j.attempts,
        s.company_id,s.search_term FROM local_environment_search_jobs j
        JOIN local_environment_search_state s ON s.term_id=j.term_id
        WHERE j.status='pending' ORDER BY CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,
        j.term_id,j.page_no LIMIT 1""").fetchone()
    if not job:
        fetched = fetch_one_candidate(db, source, user_agent)
        db.close()
        return fetched
    job_id, run_id, term_id, page_no, mode, attempts, company_id, term = job
    url = search_url(term, page_no)
    store_hint(db, config, company_id, url, f"安徽省生态环境厅精确检索：{term}", None,
               "search_response", term)
    db.commit()
    collect_source(db, source, user_agent, 1, False, url)
    row = db.execute("SELECT raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                     (SOURCE_ID, canonicalize(url))).fetchone()
    try:
        if not row:
            raise ValueError("environment search response was not saved")
        body = (ROOT / row[0]).read_bytes()
        results = result_items(body, term)
        pages = page_count(body)
        if pages > 50:
            raise ValueError(f"exact search returned implausible page count {pages}")
        skipped_old = 0
        for candidate, title, published_at, document_type in results:
            store_hint(db, config, company_id, candidate, title, published_at, document_type, term)
            enqueue_frontier(db, SOURCE_ID, candidate,
                             f"local_environment_search:{term_id}", 0)
            if published_at and published_at < config["scope"]["backfill_start"]:
                finish_frontier(db, SOURCE_ID, candidate, "skipped_scope",
                                "official search result predates backfill boundary")
                skipped_old += 1
        if page_no == 1:
            for extra_page in range(2, pages + 1):
                db.execute("""INSERT OR IGNORE INTO local_environment_search_jobs
                    (job_id,run_id,term_id,page_no,mode) VALUES(?,?,?,?,?)""",
                           (f"{run_id}:{extra_page}", run_id, term_id, extra_page, mode))
        processed_at = current.isoformat(timespec="seconds")
        db.execute("""UPDATE local_environment_search_jobs SET status='done',attempts=?,
            last_error=NULL,processed_at=? WHERE job_id=?""",
                   (attempts + 1, processed_at, job_id))
        remaining = db.execute("""SELECT 1 FROM local_environment_search_jobs
            WHERE run_id=? AND status!='done' LIMIT 1""", (run_id,)).fetchone()
        if not remaining:
            db.execute("""UPDATE local_environment_search_state SET last_completed_at=?,
                last_error=NULL WHERE term_id=?""", (processed_at, term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "environment_search_success",
               "job_id": job_id, "term_id": term_id, "search_term": term,
               "page": page_no, "page_count": pages, "results_discovered": len(results),
               "results_skipped_before_boundary": skipped_old})
    except (ValueError, OSError) as exc:
        status = "manual_review" if attempts + 1 >= 3 else "pending"
        db.execute("""UPDATE local_environment_search_jobs SET status=?,attempts=?,last_error=?
            WHERE job_id=?""", (status, attempts + 1, str(exc)[:300], job_id))
        db.execute("UPDATE local_environment_search_state SET last_error=? WHERE term_id=?",
                   (str(exc)[:300], term_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "environment_search_error",
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
