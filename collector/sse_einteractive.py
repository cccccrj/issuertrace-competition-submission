#!/usr/bin/env python3
"""Conservative backfill and daily incremental capture for SSE e-interaction Q&A."""
from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from collector.collect import ROOT, audit, canonicalize, collect_source, connect, load_sources

SOURCE_ID = "sse_einteractive"
COMPANIES = {
    "cxmt": "688825",
    "wuxi_apptec": "603259",
}
COMPANY_URL = "https://sns.sseinfo.com/company.do"
FEED_URL = "https://sns.sseinfo.com/ajax/userfeeds.do"


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS sse_einteractive_state (
        company_id TEXT PRIMARY KEY, stock_code TEXT NOT NULL, uid TEXT,
        backfill_complete INTEGER NOT NULL DEFAULT 0,
        last_incremental_date TEXT, last_error TEXT,
        backfill_coverage TEXT NOT NULL DEFAULT 'pending', coverage_start_at TEXT)""")
    state_columns = {row[1] for row in db.execute("PRAGMA table_info(sse_einteractive_state)")}
    if "backfill_coverage" not in state_columns:
        db.execute("""ALTER TABLE sse_einteractive_state ADD COLUMN backfill_coverage TEXT
            NOT NULL DEFAULT 'pending'""")
    if "coverage_start_at" not in state_columns:
        db.execute("ALTER TABLE sse_einteractive_state ADD COLUMN coverage_start_at TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS sse_einteractive_jobs (
        job_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, page_no INTEGER NOT NULL,
        mode TEXT NOT NULL, run_date TEXT, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, processed_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS sse_einteractive_items (
        item_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, company_id TEXT NOT NULL,
        stock_code TEXT NOT NULL, question_text TEXT, question_at TEXT,
        answer_text TEXT, answer_at TEXT, source_url TEXT NOT NULL,
        content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)""")
    for company_id, code in COMPANIES.items():
        db.execute("""INSERT INTO sse_einteractive_state(company_id,stock_code)
            VALUES(?,?) ON CONFLICT(company_id) DO UPDATE SET stock_code=excluded.stock_code""",
                   (company_id, code))
    for company_id, complete, coverage in db.execute("""SELECT company_id,backfill_complete,
            backfill_coverage FROM sse_einteractive_state""").fetchall():
        oldest = db.execute("""SELECT min(COALESCE(answer_at,question_at))
            FROM sse_einteractive_items WHERE company_id=?""", (company_id,)).fetchone()[0]
        if oldest:
            db.execute("UPDATE sse_einteractive_state SET coverage_start_at=? WHERE company_id=?",
                       (oldest, company_id))
        if complete and coverage == "pending":
            inferred = "partial_recent_window" if oldest else "no_public_recent_items"
            db.execute("""UPDATE sse_einteractive_state SET backfill_coverage=?,coverage_start_at=?
                WHERE company_id=?""", (inferred, oldest, company_id))
    db.commit()


def clean_fragment(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", value,
                   flags=re.I | re.S)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_cn_time(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(
        r"([12]\d{3})年(\d{1,2})月(\d{1,2})日(?:\s+(\d{1,2}):(\d{2}))?", value)
    if not match:
        return None
    year, month, day, hour, minute = match.groups()
    local = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0),
                     tzinfo=ZoneInfo("Asia/Shanghai"))
    return local.isoformat(timespec="minutes")


def parse_feed(body: bytes) -> list[dict[str, str | None]]:
    """Parse public feed fragments without depending on the site's presentation scripts."""
    text = body.decode("utf-8", errors="replace")
    starts = list(re.finditer(
        r'<div\b[^>]*class=["\'][^"\']*\bm_feed_item\b[^"\']*["\'][^>]*'
        r'id=["\']item-(\d+)["\'][^>]*>', text, re.I))
    results: list[dict[str, str | None]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.start():end]
        text_nodes = re.findall(
            r'<div\b[^>]*class=["\'][^"\']*\bm_feed_txt\b[^"\']*["\'][^>]*>(.*?)</div>',
            block, re.I | re.S)
        dates = re.findall(
            r'<div\b[^>]*class=["\'][^"\']*\bm_feed_from\b[^"\']*["\'][^>]*>.*?'
            r'<span[^>]*>\s*([^<]+?)\s*</span>', block, re.I | re.S)
        question = clean_fragment(text_nodes[0]) if text_nodes else None
        answer = clean_fragment(text_nodes[1]) if len(text_nodes) > 1 else None
        if question and question.startswith(":"):
            question = question[1:].lstrip()
        question_at = normalize_cn_time(dates[0] if dates else None)
        answer_at = normalize_cn_time(dates[1] if len(dates) > 1 else None)
        results.append({
            "item_id": start.group(1),
            "question_text": question,
            "question_at": question_at,
            "answer_text": answer,
            "answer_at": answer_at,
        })
    return results


def company_uid(body: bytes) -> str | None:
    text = body.decode("utf-8", errors="ignore")
    match = re.search(r'userfeeds\.do\?[^"\']*\buid=(\d+)', text, re.I)
    return match.group(1) if match else None


def store_feed_hint(db, config: dict, company_id: str, url: str, page_no: int) -> None:
    legal_name = config["entities"][company_id]["canonical_name"]
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,company_id,matched_legal_entity,document_type,
         original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,
        document_type=excluded.document_type,original_timezone=excluded.original_timezone,
        review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), f"上证e互动问答第{page_no}页", company_id,
         legal_name, "investor_qa_feed", "Asia/Shanghai", "candidate_entity_match"))


def ensure_due_jobs(db, today: date) -> None:
    for company_id, _code in COMPANIES.items():
        state = db.execute("""SELECT uid,backfill_complete,last_incremental_date
            FROM sse_einteractive_state WHERE company_id=?""", (company_id,)).fetchone()
        if not state or not state[0]:
            continue
        if not state[1]:
            db.execute("""INSERT OR IGNORE INTO sse_einteractive_jobs
                (job_id,company_id,page_no,mode) VALUES(?,?,1,'backfill')""",
                       (f"backfill:{company_id}:1", company_id))
        elif state[2] != today.isoformat():
            db.execute("""INSERT OR IGNORE INTO sse_einteractive_jobs
                (job_id,company_id,page_no,mode,run_date) VALUES(?,?,1,'incremental',?)""",
                       (f"incremental:{company_id}:{today.isoformat()}:1", company_id,
                        today.isoformat()))
    db.commit()


def _event_date(item: dict[str, str | None]) -> str | None:
    return item.get("answer_at") or item.get("question_at")


def process_one(contact: str, today: date | None = None) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        return 0
    source = sources[0]
    db = connect()
    ensure_schema(db)
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"

    missing = db.execute("""SELECT company_id,stock_code FROM sse_einteractive_state
        WHERE uid IS NULL ORDER BY company_id LIMIT 1""").fetchone()
    if missing:
        company_id, code = missing
        url = COMPANY_URL + "?" + urlencode({"stockcode": code})
        collect_source(db, source, user_agent, 1, False, url)
        row = db.execute("SELECT raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                         (SOURCE_ID, canonicalize(url))).fetchone()
        uid = company_uid((ROOT / row[0]).read_bytes()) if row else None
        if uid:
            db.execute("UPDATE sse_einteractive_state SET uid=?,last_error=NULL WHERE company_id=?",
                       (uid, company_id))
            audit({"source_id": SOURCE_ID, "outcome": "einteractive_company_resolved",
                   "company_id": company_id, "stock_code": code, "uid": uid})
        else:
            error = "public company page did not expose a feed uid"
            db.execute("UPDATE sse_einteractive_state SET last_error=? WHERE company_id=?",
                       (error, company_id))
            audit({"source_id": SOURCE_ID, "outcome": "einteractive_company_error",
                   "company_id": company_id, "stock_code": code, "error": error})
        db.commit()
        db.close()
        return 1

    local_today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    ensure_due_jobs(db, local_today)
    job = db.execute("""SELECT j.job_id,j.company_id,j.page_no,j.mode,j.run_date,j.attempts,
        s.stock_code,s.uid FROM sse_einteractive_jobs j
        JOIN sse_einteractive_state s ON s.company_id=j.company_id
        WHERE j.status='pending' ORDER BY CASE j.mode WHEN 'backfill' THEN 0 ELSE 1 END,
        j.page_no,j.company_id LIMIT 1""").fetchone()
    if not job:
        db.close()
        return 0
    job_id, company_id, page_no, mode, run_date, attempts, code, uid = job
    url = FEED_URL + "?" + urlencode({
        "typeCode": "company", "type": "11", "pageSize": 10, "uid": uid, "page": page_no,
    })
    store_feed_hint(db, config, company_id, url, page_no)
    db.commit()
    collect_source(db, source, user_agent, 1, False, url)
    row = db.execute("SELECT raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                     (SOURCE_ID, canonicalize(url))).fetchone()
    try:
        if not row:
            raise ValueError("e-interaction feed response was not saved")
        items = parse_feed((ROOT / row[0]).read_bytes())
        known_before = {
            item["item_id"] for item in items
            if db.execute("SELECT 1 FROM sse_einteractive_items WHERE item_id=?",
                          (item["item_id"],)).fetchone()
        }
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        boundary = config["scope"]["backfill_start"]
        stored = 0
        for item in items:
            event_at = _event_date(item)
            if mode == "backfill" and event_at and event_at[:10] < boundary:
                continue
            digest = hashlib.sha256(
                "\n".join(str(item.get(key) or "") for key in
                          ("question_text", "question_at", "answer_text", "answer_at")).encode()
            ).hexdigest()
            db.execute("""INSERT INTO sse_einteractive_items
                (item_id,source_id,company_id,stock_code,question_text,question_at,
                 answer_text,answer_at,source_url,content_hash,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                question_text=excluded.question_text,question_at=excluded.question_at,
                answer_text=excluded.answer_text,answer_at=excluded.answer_at,
                source_url=excluded.source_url,content_hash=excluded.content_hash,
                last_seen_at=excluded.last_seen_at""",
                (item["item_id"], SOURCE_ID, company_id, code, item["question_text"],
                 item["question_at"], item["answer_text"], item["answer_at"],
                 canonicalize(url), digest, now, now))
            stored += 1

        event_times = [value for value in (_event_date(item) for item in items) if value]
        dated = [value[:10] for value in event_times]
        crossed_boundary = bool(dated) and min(dated) < boundary
        if event_times:
            oldest_seen = min(event_times)
            db.execute("""UPDATE sse_einteractive_state SET coverage_start_at=CASE
                WHEN coverage_start_at IS NULL OR coverage_start_at>? THEN ?
                ELSE coverage_start_at END WHERE company_id=?""",
                       (oldest_seen, oldest_seen, company_id))
        if mode == "backfill":
            if not items or crossed_boundary:
                if crossed_boundary:
                    coverage = "configured_start_reached"
                elif page_no == 1:
                    coverage = "no_public_recent_items"
                else:
                    coverage = "partial_recent_window"
                db.execute("""UPDATE sse_einteractive_state SET backfill_complete=1,
                    backfill_coverage=?,last_error=NULL WHERE company_id=?""",
                           (coverage, company_id))
            else:
                next_page = page_no + 1
                db.execute("""INSERT OR IGNORE INTO sse_einteractive_jobs
                    (job_id,company_id,page_no,mode) VALUES(?,?,?,'backfill')""",
                           (f"backfill:{company_id}:{next_page}", company_id, next_page))
        else:
            if len(items) >= 10 and not known_before:
                next_page = page_no + 1
                db.execute("""INSERT OR IGNORE INTO sse_einteractive_jobs
                    (job_id,company_id,page_no,mode,run_date) VALUES(?,?,?,'incremental',?)""",
                           (f"incremental:{company_id}:{run_date}:{next_page}", company_id,
                            next_page, run_date))
            else:
                db.execute("""UPDATE sse_einteractive_state SET last_incremental_date=?,last_error=NULL
                    WHERE company_id=?""", (run_date, company_id))
        db.execute("""UPDATE sse_einteractive_jobs SET status='done',attempts=?,last_error=NULL,
            processed_at=? WHERE job_id=?""", (attempts + 1, now, job_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "einteractive_job_success",
               "job_id": job_id, "company_id": company_id, "page": page_no,
               "mode": mode, "items_found": len(items), "items_stored": stored,
               "boundary_reached": crossed_boundary,
               "public_history_exhausted": mode == "backfill" and not items})
    except (ValueError, OSError) as exc:
        status = "manual_review" if attempts + 1 >= 3 else "pending"
        db.execute("""UPDATE sse_einteractive_jobs SET status=?,attempts=?,last_error=?
            WHERE job_id=?""", (status, attempts + 1, str(exc)[:300], job_id))
        db.execute("UPDATE sse_einteractive_state SET last_error=? WHERE company_id=?",
                   (str(exc)[:300], company_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "einteractive_job_error",
               "job_id": job_id, "company_id": company_id, "error": str(exc)[:300]})
    finally:
        db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    process_one(contact)
