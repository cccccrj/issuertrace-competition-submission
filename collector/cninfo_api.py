#!/usr/bin/env python3
"""Incremental monthly backfill for CNINFO, the CSRC-designated disclosure site.

This exists because the Shanghai exchange's file host stopped serving documents
to anything that is not a browser: ``static.sse.com.cn`` began returning an
Aliyun ``acw_sc__v2`` javascript challenge on 2026-08-24, the collector
quarantined the host under the project's no-circumvention rule, and 531 already
discovered WuXi AppTec announcements have been stuck in ``manual_review`` ever
since.  Nothing about that decision changes here: the challenge is still live
and the host is still quarantined.

What changes is the assumption that a quarantined host is the only way to a
document.  CNINFO is not a mirror -- it is the disclosure venue the CSRC
designates for every listed company, Shanghai included -- so the same filing is
available from a second S0 source that publishes robots-absent and serves the
PDF directly.  The announcement query answers only to POST, which is why
``collect_source`` learned to submit a form; routing it through that function
rather than calling requests here keeps the robots check, the redirect-by-hop
SSRF check, the response cap, the challenge detector, the rate gate and the
audit trail in force.

The exchange stays the citable origin.  ``align_with_sse`` matches what arrives
here against the stranded SSE queue by (company, date, title) and marks the
matched rows ``superseded`` with the CNINFO URL that replaced them, so the
provenance chain reads "published by the SSE, obtained from CNINFO" rather than
quietly swapping one for the other.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

from collector.collect import (
    AUDIT_LOG, ROOT, active_cooldown, api_job_outcome, audit, blocked_host, canonicalize, collect_source,
    connect, enqueue_frontier, finish_frontier, load_sources, now_iso,
)

SOURCE_ID = "cninfo_announcements"
ENDPOINT = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
FILE_HOST = "https://static.cninfo.com.cn/"
SSE_SOURCE_ID = "sse_announcements"
# Namespaces this adapter's rows in the shared api_jobs key space.
JOB_PREFIX = "cninfo"
PAGE_SIZE = 30
# The value the source inventory declares and the evidence index keys on.  The
# singular reads better and indexes nothing: claim_index looks for
# "announcements", so a filing typed "announcement" is collected, parsed, and
# then silently absent from every citation.
DOCUMENT_TYPE = "announcements"
# The query responses are audit trail, not evidence.  Typed like this they are
# excluded from labelling and from the evidence index by name rather than by
# happening to lack page anchors.
QUERY_DOCUMENT_TYPE = "search_response"

# The query needs CNINFO's internal organisation id as well as the ticker; a
# code alone returns an empty page rather than an error.  Both were resolved
# once through the site's own topSearch endpoint and are recorded here so that
# routine collection does not depend on a second interface.
SECURITIES = {
    "cxmt": {"code": "688825", "org_id": "9920000008", "column": "sse", "plate": "sh"},
    "wuxi_apptec": {"code": "603259", "org_id": "9900035584", "column": "sse", "plate": "sh"},
}


def ensure_jobs(db, config: dict, local_now: datetime | None = None) -> None:
    """One job per company-month, then one per day since the snapshot date.

    ``api_jobs.job_id`` is a primary key shared with every other API adapter, so
    the id carries this source's own prefix.  Without it a month here collides
    with the same month under ``sse_announcements``, ``INSERT OR IGNORE`` drops
    the row, and the backfill silently plans nothing -- which is exactly what
    happened the first time this ran.
    """
    db.execute("""CREATE TABLE IF NOT EXISTS api_jobs (
        source_id TEXT NOT NULL, job_id TEXT PRIMARY KEY, company_id TEXT NOT NULL,
        security_code TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
        page_no INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, processed_at TEXT)""")
    start = date.fromisoformat(config["scope"]["backfill_start"])
    end = date.fromisoformat(config["scope"]["snapshot_as_of"])
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        month_end = min(date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1]), end)
        for company_id, security in SECURITIES.items():
            key = f"{JOB_PREFIX}:{company_id}:{cursor.isoformat()}:{month_end.isoformat()}:1"
            db.execute("""INSERT OR IGNORE INTO api_jobs
                (source_id,job_id,company_id,security_code,start_date,end_date,page_no)
                VALUES(?,?,?,?,?,?,1)""",
                       (SOURCE_ID, key, company_id, security["code"], cursor.isoformat(), month_end.isoformat()))
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    incremental_day = end + timedelta(days=1)
    local_now = local_now or datetime.now(ZoneInfo(config["scope"].get("incremental_timezone", "Asia/Shanghai")))
    while incremental_day <= local_now.date():
        for company_id, security in SECURITIES.items():
            key = f"incremental:{JOB_PREFIX}:{company_id}:{incremental_day.isoformat()}:1"
            db.execute("""INSERT OR IGNORE INTO api_jobs
                (source_id,job_id,company_id,security_code,start_date,end_date,page_no)
                VALUES(?,?,?,?,?,?,1)""",
                       (SOURCE_ID, key, company_id, security["code"],
                        incremental_day.isoformat(), incremental_day.isoformat()))
        incremental_day += timedelta(days=1)
    # Today's incremental job is re-opened once an hour: a filing published this
    # afternoon must not wait for tomorrow's job to be created.
    current_prefix = f"incremental:{JOB_PREFIX}:%:{local_now.date().isoformat()}:%"
    current_hour = local_now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for job_id, processed_at in db.execute(
            "SELECT job_id,processed_at FROM api_jobs WHERE source_id=? AND job_id LIKE ? AND status='done'",
            (SOURCE_ID, current_prefix)).fetchall():
        if processed_at and datetime.fromisoformat(processed_at) < current_hour:
            db.execute("UPDATE api_jobs SET status='pending' WHERE job_id=?", (job_id,))
    db.commit()


def query_params(company_id: str, start_date: str, end_date: str, page_no: int) -> dict:
    security = SECURITIES[company_id]
    return {
        "pageNum": page_no, "pageSize": PAGE_SIZE, "column": security["column"],
        "tabName": "fulltext", "plate": security["plate"],
        "stock": f"{security['code']},{security['org_id']}",
        "seDate": f"{start_date}~{end_date}", "isHLtitle": "true", "category": "",
    }


def announcement_date(item: dict) -> str:
    """Publication date in the exchange's own timezone, not the server's."""
    stamp = item.get("announcementTime")
    if stamp is None:
        return ""
    return datetime.fromtimestamp(int(stamp) / 1000, ZoneInfo("Asia/Shanghai")).date().isoformat()


def clean_title(raw: str | None) -> str:
    """Drop the search highlighting the interface injects into matched titles."""
    return re.sub(r"</?em>", "", raw or "").strip()


def pdf_url(item: dict) -> str | None:
    adjunct = (item.get("adjunctUrl") or "").strip()
    return canonicalize(urljoin(FILE_HOST, adjunct)) if adjunct else None


def normalized_title(title: str) -> str:
    """Titles differ between venues only by punctuation and spacing, not words."""
    return re.sub(r"[\s　:：()（）【】\[\]。，,、_-]+", "", title or "").casefold()


def align_with_sse(db, company_id: str, published_at: str, title: str, cninfo_url: str) -> str | None:
    """Retire the stranded exchange URL that this document replaces.

    Matched on company, publication date and normalised title -- the three the
    two venues agree on.  The exchange row keeps its URL and gains a reason
    naming what superseded it, so the queue shrinks by documents obtained, not
    by documents forgotten.
    """
    rows = db.execute("""SELECT d.canonical_url, d.title FROM discovered_metadata d
        JOIN crawl_frontier f ON f.source_id=? AND f.url=d.canonical_url
        WHERE d.source_id=? AND d.company_id=? AND d.published_at=? AND f.status='manual_review'""",
        (SSE_SOURCE_ID, SSE_SOURCE_ID, company_id, published_at)).fetchall()
    target = normalized_title(title)
    for sse_url, sse_title in rows:
        if normalized_title(sse_title) == target:
            finish_frontier(db, SSE_SOURCE_ID, sse_url, "superseded",
                            f"obtained from {SOURCE_ID}: {cninfo_url}")
            return sse_url
    return None


QUERY_HINT_SQL = """INSERT INTO discovered_metadata
    (source_id,canonical_url,title,document_type,original_timezone)
    VALUES(?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
    document_type=excluded.document_type"""


def page_count_for(payload: dict) -> int:
    """How many pages this query really has.

    The interface reports ``totalpages``, and it lies: a month with 38 filings
    comes back as ``totalRecordNum: 38, hasMore: true, totalpages: 1``.  Trusting
    it silently truncated every busy month to its first 30 announcements -- the
    kind of gap that looks like "the venue does not have this document" rather
    than like a bug.  The record count is the honest number; ``hasMore`` is the
    fallback when even that is missing.
    """
    total = int(payload.get("totalRecordNum") or 0)
    if total:
        return max(1, -(-total // PAGE_SIZE))
    if payload.get("hasMore") and payload.get("announcements"):
        return 2
    return 1


def process_one(contact: str) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    source = sources[0]
    db = connect()
    ensure_jobs(db, config)
    source_state = db.execute("SELECT state FROM source_state WHERE source_id=?", (SOURCE_ID,)).fetchone()
    if ((source_state and source_state[0] == "manual_review") or active_cooldown(db, SOURCE_ID)
            or blocked_host(db, SOURCE_ID, "www.cninfo.com.cn")):
        db.close()
        return 0
    job = db.execute("""SELECT job_id,company_id,security_code,start_date,end_date,page_no,attempts
        FROM api_jobs WHERE source_id=? AND status='pending' ORDER BY start_date,page_no,company_id LIMIT 1""",
        (SOURCE_ID,)).fetchone()
    if not job:
        db.close()
        return 0
    job_id, company_id, code, start_date, end_date, page_no, attempts = job
    params = query_params(company_id, start_date, end_date, page_no)
    # The parameters travel twice: in the form that the interface answers, and
    # in the URL that identifies this response in ``documents``.  A POST has no
    # natural identity, and a stored response nobody can name is not evidence.
    url = ENDPOINT + "?" + urlencode(params)
    db.execute(QUERY_HINT_SQL,
               (SOURCE_ID, canonicalize(url),
                f"CNINFO 公告查询 {company_id} {start_date}~{end_date} p{page_no}",
                QUERY_DOCUMENT_TYPE, "Asia/Shanghai"))
    db.commit()
    collect_source(db, source, f"ListedCompanyEventIntelligence/0.1 (+{contact})", 1, False, url, params)
    row = db.execute("SELECT raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                     (SOURCE_ID, canonicalize(url))).fetchone()
    result = 1
    try:
        raw = (ROOT / row[0]).read_text(encoding="utf-8") if row else ""
        payload = json.loads(raw)
        announcements = payload.get("announcements") or []
        entities = config["entities"]
        legal_name = entities[company_id]["canonical_name"]
        discovered = superseded = 0
        for item in announcements:
            target = pdf_url(item)
            if not target:
                continue
            title = clean_title(item.get("announcementTitle"))
            published_at = announcement_date(item)
            db.execute("""INSERT INTO discovered_metadata
                (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,document_type,original_timezone)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
                title=excluded.title,published_at=excluded.published_at,company_id=excluded.company_id,
                matched_legal_entity=excluded.matched_legal_entity,document_type=excluded.document_type,
                original_timezone=excluded.original_timezone""",
                (SOURCE_ID, target, title, published_at, company_id, legal_name,
                 DOCUMENT_TYPE, "Asia/Shanghai"))
            enqueue_frontier(db, SOURCE_ID, target, canonicalize(url), 0)
            host_reason = blocked_host(db, SOURCE_ID, "static.cninfo.com.cn")
            if host_reason:
                finish_frontier(db, SOURCE_ID, target, "manual_review", host_reason)
            discovered += 1
            if align_with_sse(db, company_id, published_at, title, target):
                superseded += 1
        page_count = page_count_for(payload)
        for extra_page in range(page_no + 1, page_count + 1):
            extra_id = job_id.rsplit(":", 1)[0] + f":{extra_page}"
            db.execute("""INSERT OR IGNORE INTO api_jobs
                (source_id,job_id,company_id,security_code,start_date,end_date,page_no)
                VALUES(?,?,?,?,?,?,?)""",
                       (SOURCE_ID, extra_id, company_id, code, start_date, end_date, extra_page))
        db.execute("UPDATE api_jobs SET status='done',attempts=?,last_error=NULL,processed_at=? WHERE job_id=?",
                   (attempts + 1, datetime.now(timezone.utc).isoformat(timespec="seconds"), job_id))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": api_job_outcome(job_id, True), "job_id": job_id,
               "security_code": code, "items_discovered": discovered,
               "sse_rows_superseded": superseded, "page_count": page_count})
    except (ValueError, KeyError, json.JSONDecodeError, OSError) as exc:
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


def next_pending_document(db, skip: set[str]):
    """The oldest announcement discovered here that has not been retrieved yet."""
    for url, in db.execute("""SELECT url FROM crawl_frontier
        WHERE source_id=? AND status='pending' ORDER BY discovered_at,url""", (SOURCE_ID,)):
        if url not in skip:
            return url
    return None


def last_refusal(url: str, tail_bytes: int = 2_000_000) -> str | None:
    """The collector's own last word on this URL, from the audit log.

    ``collect_source`` records why it would not keep a response -- an oversize
    body, an unsafe redirect -- but returns nothing, so the caller otherwise has
    only "no document appeared" to go on.  A queue entry whose reason is "not
    saved" tells a person nothing and invites a pointless retry; the real reason
    is one line away and other readers of this log (health, the audit page,
    report) already take it from here.
    """
    try:
        size = AUDIT_LOG.stat().st_size
        with AUDIT_LOG.open("rb") as handle:
            handle.seek(max(0, size - tail_bytes))
            lines = handle.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("url") == url and event.get("outcome") != "cninfo_document_error":
            reason = event.get("error") or event.get("outcome")
            return f"{event.get('outcome')}: {reason}" if event.get("error") else str(reason)
    return None


def download_one(contact: str, skip: set[str]) -> str | None:
    """Retrieve one discovered announcement, through the same guards as any page.

    The frontier is the queue -- these rows were written by the discovery pass --
    so success and failure are recorded there rather than in a table of this
    adapter's own.  A URL that fails is left pending with the reason attached and
    handed back to the caller, which stops asking for it after a few tries; the
    collector's own cooldown and quarantine states end the run before that.
    """
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    source = sources[0]
    db = connect()
    try:
        state = db.execute("SELECT state FROM source_state WHERE source_id=?", (SOURCE_ID,)).fetchone()
        if ((state and state[0] == "manual_review") or active_cooldown(db, SOURCE_ID)
                or blocked_host(db, SOURCE_ID, "static.cninfo.com.cn")):
            return None
        url = next_pending_document(db, skip)
        if not url:
            return None
        collect_source(db, source, f"ListedCompanyEventIntelligence/0.1 (+{contact})", 1, False, url)
        saved = db.execute("SELECT status,raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                           (SOURCE_ID, canonicalize(url))).fetchone()
        after = db.execute("SELECT state,last_error FROM source_state WHERE source_id=?", (SOURCE_ID,)).fetchone()
        if saved and saved[0] == 200 and saved[1] and after and after[0] == "closed":
            finish_frontier(db, SOURCE_ID, url, "done")
            audit({"source_id": SOURCE_ID, "outcome": "cninfo_document_success", "url": url,
                   "raw_path": saved[1]})
        elif after and after[0] in {"manual_review", "cooldown", "circuit_open"}:
            # The source is in trouble, not this document: leave it queued.
            error = (after[1] or "source unavailable")[:300]
            finish_frontier(db, SOURCE_ID, url, "manual_review" if after[0] == "manual_review"
                            else "pending", error)
            audit({"source_id": SOURCE_ID, "outcome": "cninfo_document_error", "url": url, "error": error})
            return url
        else:
            # The source is healthy and the response still was not kept, so the
            # refusal is about this document -- an oversize body, most often.
            # Retrying cannot change that; it belongs in the manual queue with
            # the real reason rather than pending forever with a vague one.
            error = (last_refusal(url) or "response was not saved")[:300]
            finish_frontier(db, SOURCE_ID, url, "manual_review", error)
            audit({"source_id": SOURCE_ID, "outcome": "cninfo_document_error", "url": url, "error": error})
            return url
        return url
    finally:
        db.close()


# A backfill of several hundred documents will meet a transient 5xx sooner or
# later, and one is enough to put the source in a short cooldown.  Waiting it out
# is the point of the cooldown; abandoning the run is not.
MAX_COOLDOWN_WAIT_SECONDS = 900


def cooldown_wait(db) -> float | None:
    """Seconds until the source may be asked again, or None if it may not be."""
    row = db.execute("SELECT state,cooldown_until FROM source_state WHERE source_id=?",
                     (SOURCE_ID,)).fetchone()
    if not row or row[0] == "manual_review" or not row[1]:
        return None
    remaining = (datetime.fromisoformat(row[1]) - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return 0.0
    return remaining if remaining <= MAX_COOLDOWN_WAIT_SECONDS else None


def drain_documents(contact: str, limit: int) -> dict[str, int]:
    attempts: dict[str, int] = {}
    skip: set[str] = set()
    fetched = failed = waits = 0
    while fetched + failed < limit:
        db = connect()
        try:
            pending_before = db.execute(
                "SELECT count(*) FROM crawl_frontier WHERE source_id=? AND status='pending'",
                (SOURCE_ID,)).fetchone()[0]
            waiting = cooldown_wait(db)
        finally:
            db.close()
        if not pending_before:
            break
        if waiting:
            time.sleep(waiting + 1)
            waits += 1
            continue
        url = download_one(contact, skip)
        if url is None:
            break
        db = connect()
        try:
            status = db.execute("SELECT status FROM crawl_frontier WHERE source_id=? AND url=?",
                                (SOURCE_ID, url)).fetchone()
        finally:
            db.close()
        if status and status[0] == "done":
            fetched += 1
            continue
        if status and status[0] != "pending":
            # Quarantined or otherwise closed out: it will not be offered again,
            # so count it now rather than waiting for retries that cannot happen.
            failed += 1
            continue
        attempts[url] = attempts.get(url, 0) + 1
        if attempts[url] >= 3:
            skip.add(url)
            failed += 1
    return {"fetched": fetched, "failed": failed, "cooldown_waits": waits}


def drain_jobs(contact: str, limit: int) -> int:
    """Work through the discovery queue, waiting out transient cooldowns.

    ``process_one`` reports "nothing done" both when the queue is empty and when
    the source is briefly cooling off after a 5xx.  Treating those the same ended
    a 88-job sweep at job 48 on one HTTP 504 -- and a sweep that stops early does
    not look broken, it looks like the venue has fewer filings than it does.
    """
    done = 0
    while done < limit:
        outcome = process_one(contact)
        if outcome > 0:
            done += 1
            continue
        db = connect()
        try:
            pending = db.execute(
                "SELECT count(*) FROM api_jobs WHERE source_id=? AND status='pending'",
                (SOURCE_ID,)).fetchone()[0]
            waiting = cooldown_wait(db)
        finally:
            db.close()
        if not pending or not waiting:
            break
        time.sleep(waiting + 1)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-jobs", type=int, default=1,
                        help="stop after this many discovery jobs (default: 1, as the scheduler runs it; "
                             "0 skips discovery and goes straight to --download)")
    parser.add_argument("--download", type=int, default=0, metavar="N",
                        help="after discovery, retrieve up to N announcements already discovered")
    args = parser.parse_args()
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    done = drain_jobs(contact, max(0, args.max_jobs))
    summary = {"source_id": SOURCE_ID, "jobs_processed": done, "at": now_iso()}
    if args.download:
        summary["documents"] = drain_documents(contact, args.download)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
