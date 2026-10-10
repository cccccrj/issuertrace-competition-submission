#!/usr/bin/env python3
"""Incremental monthly backfill for the SSE public announcement interface."""
from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

from collector.collect import (
    ROOT, active_cooldown, api_job_outcome, audit, blocked_host, canonicalize, collect_source, connect,
    enqueue_frontier, finish_frontier, load_sources,
)

SOURCE_ID = "sse_announcements"
ENDPOINT = "https://query.sse.com.cn/security/stock/queryCompanyBulletinNew.do"


def ensure_jobs(db, config: dict, local_now: datetime | None = None) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS api_jobs (
        source_id TEXT NOT NULL, job_id TEXT PRIMARY KEY, company_id TEXT NOT NULL,
        security_code TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
        page_no INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT)""")
    start = date.fromisoformat(config["scope"]["backfill_start"])
    end = date.fromisoformat(config["scope"]["snapshot_as_of"])
    codes = {"cxmt": "688825", "wuxi_apptec": "603259"}
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        month_end = min(date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1]), end)
        for company_id, code in codes.items():
            key = f"{company_id}:{cursor.isoformat()}:{month_end.isoformat()}:1"
            db.execute("""INSERT OR IGNORE INTO api_jobs
                (source_id,job_id,company_id,security_code,start_date,end_date,page_no)
                VALUES(?,?,?,?,?,?,1)""", (SOURCE_ID, key, company_id, code, cursor.isoformat(), month_end.isoformat()))
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    incremental_day = end + timedelta(days=1)
    local_now = local_now or datetime.now(ZoneInfo(config["scope"].get("incremental_timezone", "Asia/Shanghai")))
    while incremental_day <= local_now.date():
        for company_id, code in codes.items():
            key = f"incremental:{company_id}:{incremental_day.isoformat()}:1"
            db.execute("""INSERT OR IGNORE INTO api_jobs
                (source_id,job_id,company_id,security_code,start_date,end_date,page_no)
                VALUES(?,?,?,?,?,?,1)""",
                       (SOURCE_ID, key, company_id, code,
                        incremental_day.isoformat(), incremental_day.isoformat()))
        incremental_day += timedelta(days=1)
    current_prefix = f"incremental:%:{local_now.date().isoformat()}:%"
    current_hour = local_now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = db.execute("""SELECT job_id,processed_at FROM api_jobs
        WHERE job_id LIKE ? AND status='done'""", (current_prefix,)).fetchall()
    for job_id, processed_at in rows:
        if processed_at and datetime.fromisoformat(processed_at) < current_hour:
            db.execute("UPDATE api_jobs SET status='pending' WHERE job_id=?", (job_id,))
    db.commit()


def flatten_items(value):
    if isinstance(value, dict):
        if value.get("URL") and value.get("SECURITY_CODE"):
            yield value
        else:
            for child in value.values():
                yield from flatten_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from flatten_items(child)


def process_one(contact: str) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    source = sources[0]
    db = connect()
    ensure_jobs(db, config)
    source_state = db.execute("SELECT state FROM source_state WHERE source_id=?", (SOURCE_ID,)).fetchone()
    if ((source_state and source_state[0] == "manual_review") or active_cooldown(db, SOURCE_ID)
            or blocked_host(db, SOURCE_ID, "query.sse.com.cn")):
        db.close()
        return 0
    job = db.execute("""SELECT job_id,company_id,security_code,start_date,end_date,page_no,attempts
        FROM api_jobs WHERE source_id=? AND status='pending' ORDER BY start_date,page_no,company_id LIMIT 1""", (SOURCE_ID,)).fetchone()
    if not job:
        db.close()
        return 0
    job_id, company_id, code, start_date, end_date, page_no, attempts = job
    callback = "jsonp" + hashlib.sha256(job_id.encode()).hexdigest()[:12]
    params = {
        "isPagination": "true", "pageHelp.pageSize": 100, "pageHelp.pageNo": page_no,
        "pageHelp.beginPage": page_no, "pageHelp.cacheSize": 1, "pageHelp.endPage": page_no,
        "START_DATE": start_date, "END_DATE": end_date, "SECURITY_CODE": code,
        "TITLE": "", "BULLETIN_TYPE": "", "stockType": "", "jsonCallBack": callback,
    }
    url = ENDPOINT + "?" + urlencode(params)
    collect_source(db, source, f"ListedCompanyEventIntelligence/0.1 (+{contact})", 1, False, url)
    row = db.execute("SELECT raw_path FROM documents WHERE source_id=? AND canonical_url=?", (SOURCE_ID, canonicalize(url))).fetchone()
    result = 1
    try:
        raw = (ROOT / row[0]).read_text(encoding="utf-8") if row else ""
        start_obj, end_obj = raw.find("{"), raw.rfind("}")
        payload = json.loads(raw[start_obj:end_obj + 1])
        if str(payload.get("success", "true")).lower() == "false":
            raise ValueError(payload.get("error", "SSE API returned success=false"))
        unique = {}
        for item in flatten_items(payload.get("result", [])):
            unique[item["URL"]] = item
        entities = config["entities"]
        legal_name = entities[company_id]["canonical_name"]
        for item in unique.values():
            pdf_url = canonicalize(urljoin("https://static.sse.com.cn", item["URL"]))
            db.execute("""INSERT INTO discovered_metadata
                (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,document_type,original_timezone)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
                title=excluded.title,published_at=excluded.published_at,company_id=excluded.company_id,
                matched_legal_entity=excluded.matched_legal_entity,document_type=excluded.document_type,
                original_timezone=excluded.original_timezone""",
                (SOURCE_ID, pdf_url, item.get("TITLE"), item.get("SSEDATE"), company_id, legal_name,
                 "announcement", "Asia/Shanghai"))
            enqueue_frontier(db, SOURCE_ID, pdf_url, canonicalize(url), 0)
            host_reason = blocked_host(db, SOURCE_ID, "static.sse.com.cn")
            if host_reason:
                finish_frontier(db, SOURCE_ID, pdf_url, "manual_review", host_reason)
        page_help = payload.get("pageHelp") or {}
        page_count = int(page_help.get("pageCount") or 1)
        for extra_page in range(page_no + 1, page_count + 1):
            extra_id = job_id.rsplit(":", 1)[0] + f":{extra_page}"
            db.execute("""INSERT OR IGNORE INTO api_jobs
                (source_id,job_id,company_id,security_code,start_date,end_date,page_no)
                VALUES(?,?,?,?,?,?,?)""", (SOURCE_ID, extra_id, company_id, code, start_date, end_date, extra_page))
        db.execute("UPDATE api_jobs SET status='done',attempts=?,last_error=NULL,processed_at=? WHERE job_id=?",
                   (attempts + 1, datetime.now(timezone.utc).isoformat(timespec="seconds"), job_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": api_job_outcome(job_id, True), "job_id": job_id,
               "security_code": code, "items_discovered": len(unique), "page_count": page_count})
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        status = "manual_review" if attempts + 1 >= 3 else "pending"
        db.execute("UPDATE api_jobs SET status=?,attempts=?,last_error=? WHERE job_id=?",
                   (status, attempts + 1, str(exc)[:300], job_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": api_job_outcome(job_id, False), "job_id": job_id,
               "error": str(exc)[:300]})
        result = -1
    finally:
        db.close()
    return result


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    process_one(contact)
