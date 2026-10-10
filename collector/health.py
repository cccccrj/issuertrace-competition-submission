#!/usr/bin/env python3
"""Read-only health check for the collection daemon and persistent queues."""
from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
from datetime import datetime, timezone

from collector.collect import AUDIT_LOG, STATE_DB
from collector.scheduler import LOCK


def daemon_running() -> bool:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def last_audit_event() -> dict | None:
    if not AUDIT_LOG.exists():
        return None
    last = None
    with AUDIT_LOG.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
    return last


def api_jobs_by_class(db: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = db.execute("""SELECT CASE WHEN job_id LIKE 'incremental:%'
        THEN 'incremental' ELSE 'backfill' END AS job_class,status,count(*)
        FROM api_jobs GROUP BY job_class,status ORDER BY job_class,status""").fetchall()
    result: dict[str, dict[str, int]] = {}
    for job_class, status, count in rows:
        result.setdefault(job_class, {})[status] = count
    return result


def maintenance_failure_reasons(maintenance: dict[str, dict[str, str]]) -> list[str]:
    reasons = []
    for job_id, state in maintenance.items():
        if state.get("status", "").endswith("_error"):
            reasons.append(f"maintenance job {job_id} last run failed")
    return reasons


def csrc_search_job_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        company_id: {
            "status": status, "attempts": attempts,
            "last_run_at": last_run_at, "last_error": last_error,
        }
        for company_id, status, attempts, last_run_at, last_error in db.execute(
            """SELECT company_id,status,attempts,last_run_at,last_error
            FROM csrc_search_jobs ORDER BY company_id""")
    }


def einteractive_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        company_id: {
            "stock_code": stock_code, "uid": uid,
            "backfill_coverage": coverage, "coverage_start_at": coverage_start,
            "last_incremental_date": incremental_date, "items": item_count,
            "last_error": last_error,
        }
        for company_id, stock_code, uid, coverage, coverage_start, incremental_date,
            item_count, last_error in db.execute("""SELECT s.company_id,s.stock_code,s.uid,
                s.backfill_coverage,s.coverage_start_at,s.last_incremental_date,
                count(i.item_id),s.last_error FROM sse_einteractive_state s
                LEFT JOIN sse_einteractive_items i ON i.company_id=s.company_id
                GROUP BY s.company_id,s.stock_code,s.uid,s.backfill_coverage,
                    s.coverage_start_at,s.last_incremental_date,s.last_error
                ORDER BY s.company_id""")
    }


def environment_search_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {
            "company_id": company_id, "search_term": search_term,
            "last_completed_at": last_completed_at, "last_error": last_error,
        }
        for term_id, company_id, search_term, last_completed_at, last_error in db.execute(
            """SELECT term_id,company_id,search_term,last_completed_at,last_error
            FROM local_environment_search_state ORDER BY term_id""")
    }


def miit_search_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {
            "search_term": search_term, "last_completed_at": last_completed_at,
            "total_results": total_results, "last_error": last_error,
        }
        for term_id, search_term, last_completed_at, total_results, last_error in db.execute(
            """SELECT term_id,search_term,last_completed_at,total_results,last_error
            FROM miit_search_state ORDER BY term_id""")
    }


def bis_search_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {
            "company_id": company_id, "search_term": search_term,
            "last_completed_at": last_completed_at, "total_results": total_results,
            "last_error": last_error,
        }
        for term_id, company_id, search_term, last_completed_at, total_results,
            last_error in db.execute("""SELECT term_id,company_id,search_term,
                last_completed_at,total_results,last_error
                FROM bis_search_state ORDER BY term_id""")
    }


def openfda_enforcement_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {
            "endpoint_type": endpoint_type, "company_id": company_id,
            "search_term": search_term, "last_completed_at": last_completed_at,
            "total_results": total_results, "last_error": last_error,
        }
        for term_id, endpoint_type, company_id, search_term, last_completed_at,
            total_results, last_error in db.execute("""SELECT term_id,endpoint_type,
                company_id,search_term,last_completed_at,total_results,last_error
                FROM openfda_enforcement_state ORDER BY term_id""")
    }


def nbd_search_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {"company_id": company_id, "search_term": search_term,
                  "last_completed_at": completed, "total_results": total,
                  "exact_results": exact, "last_error": error}
        for term_id, company_id, search_term, completed, total, exact, error in db.execute(
            """SELECT term_id,company_id,search_term,last_completed_at,total_results,
            exact_results,last_error FROM nbd_search_state ORDER BY term_id""")
    }


def twenty_first_search_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {"company_id": company_id, "search_term": search_term,
                  "last_completed_at": completed, "pages_scanned": pages,
                  "exact_results": exact, "skipped_before_window": skipped,
                  "last_error": error}
        for term_id, company_id, search_term, completed, pages, exact, skipped, error in db.execute(
            """SELECT term_id,company_id,search_term,last_completed_at,pages_scanned,
            exact_results,skipped_before_window,last_error
            FROM twenty_first_search_state ORDER BY term_id""")
    }


def jiemian_search_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {"company_id": company_id, "search_term": search_term,
                  "last_completed_at": completed, "pages_scanned": pages,
                  "exact_results": exact, "skipped_before_window": skipped,
                  "coverage_note": coverage, "last_error": error}
        for term_id, company_id, search_term, completed, pages, exact, skipped,
            coverage, error in db.execute(
            """SELECT term_id,company_id,search_term,last_completed_at,pages_scanned,
            exact_results,skipped_before_window,coverage_note,last_error
            FROM jiemian_search_state ORDER BY term_id""")
    }


def yicai_search_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {"company_id": company_id, "search_term": search_term,
                  "last_completed_at": completed, "pages_scanned": pages,
                  "exact_results": exact, "skipped_before_window": skipped,
                  "coverage_note": coverage, "last_error": error}
        for term_id, company_id, search_term, completed, pages, exact, skipped,
            coverage, error in db.execute(
            """SELECT term_id,company_id,search_term,last_completed_at,pages_scanned,
            exact_results,skipped_before_window,coverage_note,last_error
            FROM yicai_search_state ORDER BY term_id""")
    }


def stcn_search_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {"company_id": company_id, "search_term": search_term,
                  "last_completed_at": completed, "pages_scanned": pages,
                  "exact_results": exact, "skipped_before_window": skipped,
                  "coverage_note": coverage, "last_error": error}
        for term_id, company_id, search_term, completed, pages, exact, skipped,
            coverage, error in db.execute(
            """SELECT term_id,company_id,search_term,last_completed_at,pages_scanned,
            exact_results,skipped_before_window,coverage_note,last_error
            FROM stcn_search_state ORDER BY term_id""")
    }


def cnstock_search_health(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {
        term_id: {"company_id": company_id, "search_term": search_term,
                  "last_completed_at": completed, "total_results": total,
                  "pages_scanned": pages, "exact_results": exact,
                  "skipped_before_window": skipped,
                  "coverage_note": coverage, "last_error": error}
        for term_id, company_id, search_term, completed, total, pages, exact, skipped,
            coverage, error in db.execute(
            """SELECT term_id,company_id,search_term,last_completed_at,total_results,
            pages_scanned,exact_results,skipped_before_window,coverage_note,last_error
            FROM cnstock_search_state ORDER BY term_id""")
    }


def snapshot() -> dict:
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "daemon_running": daemon_running(),
        "last_event": last_audit_event(),
        "documents": 0,
        "versions": 0,
        "attachments": 0,
        "frontier": {},
        "api_jobs": {},
        "api_jobs_by_class": {},
        "federal_register_jobs": {},
        "csrc_search_jobs": {},
        "sse_einteractive": {},
        "sse_einteractive_jobs": {},
        "local_environment_search": {},
        "local_environment_search_jobs": {},
        "miit_search": {},
        "miit_search_jobs": {},
        "bis_search": {},
        "bis_search_jobs": {},
        "openfda_enforcement": {},
        "openfda_enforcement_jobs": {},
        "openfda_enforcement_items": 0,
        "govinfo_package_jobs": {},
        "ir_material_access_jobs": {},
        "ir_material_scan_pages": 0,
        "cfm_producer_jobs": {},
        "cfm_producer_state": {},
        "nbd_search": {},
        "nbd_search_jobs": {},
        "nbd_detail_jobs": {},
        "twenty_first_search": {},
        "twenty_first_search_jobs": {},
        "twenty_first_detail_jobs": {},
        "jiemian_search": {},
        "jiemian_search_jobs": {},
        "jiemian_detail_jobs": {},
        "yicai_search": {},
        "yicai_search_jobs": {},
        "yicai_detail_jobs": {},
        "stcn_search": {},
        "stcn_search_jobs": {},
        "stcn_detail_jobs": {},
        "cnstock_search": {},
        "cnstock_search_jobs": {},
        "cnstock_detail_jobs": {},
        "fierce_search": {},
        "fierce_search_jobs": {},
        "fierce_detail_jobs": {},
        "biospace_sitemap": {},
        "biospace_sitemap_jobs": {},
        "biospace_detail_jobs": {},
        "toms_sitemap": {},
        "toms_sitemap_jobs": {},
        "toms_detail_jobs": {},
        "trendforce_archive": {},
        "trendforce_archive_jobs": {},
        "trendforce_detail_jobs": {},
        "ijiwei_latest": {},
        "ijiwei_detail_jobs": {},
        "maintenance": {},
    }
    if not STATE_DB.exists():
        return result
    db = sqlite3.connect(STATE_DB)
    result["documents"] = db.execute("SELECT count(*) FROM documents").fetchone()[0]
    result["versions"] = db.execute("SELECT count(*) FROM document_versions").fetchone()[0]
    result["attachments"] = db.execute(
        "SELECT count(*) FROM document_versions WHERE attachment_hash IS NOT NULL").fetchone()[0]
    result["frontier"] = dict(db.execute(
        "SELECT status,count(*) FROM crawl_frontier GROUP BY status").fetchall())
    for table in ("api_jobs", "federal_register_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if result["api_jobs"]:
        result["api_jobs_by_class"] = api_jobs_by_class(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='csrc_search_jobs'").fetchone():
        result["csrc_search_jobs"] = csrc_search_job_health(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sse_einteractive_state'").fetchone():
        result["sse_einteractive"] = einteractive_health(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sse_einteractive_jobs'").fetchone():
        result["sse_einteractive_jobs"] = dict(db.execute(
            "SELECT status,count(*) FROM sse_einteractive_jobs GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_environment_search_state'").fetchone():
        result["local_environment_search"] = environment_search_health(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_environment_search_jobs'").fetchone():
        result["local_environment_search_jobs"] = dict(db.execute(
            "SELECT status,count(*) FROM local_environment_search_jobs GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='miit_search_state'").fetchone():
        result["miit_search"] = miit_search_health(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='miit_search_jobs'").fetchone():
        result["miit_search_jobs"] = dict(db.execute(
            "SELECT status,count(*) FROM miit_search_jobs GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bis_search_state'").fetchone():
        result["bis_search"] = bis_search_health(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bis_search_jobs'").fetchone():
        result["bis_search_jobs"] = dict(db.execute(
            "SELECT status,count(*) FROM bis_search_jobs GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='openfda_enforcement_state'").fetchone():
        result["openfda_enforcement"] = openfda_enforcement_health(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='openfda_enforcement_jobs'").fetchone():
        result["openfda_enforcement_jobs"] = dict(db.execute(
            "SELECT status,count(*) FROM openfda_enforcement_jobs GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='openfda_enforcement_items'").fetchone():
        result["openfda_enforcement_items"] = db.execute(
            "SELECT count(*) FROM openfda_enforcement_items").fetchone()[0]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='govinfo_package_jobs'").fetchone():
        result["govinfo_package_jobs"] = dict(db.execute(
            "SELECT status,count(*) FROM govinfo_package_jobs GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ir_material_access_jobs'").fetchone():
        result["ir_material_access_jobs"] = dict(db.execute(
            "SELECT status,count(*) FROM ir_material_access_jobs GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ir_material_scan_state'").fetchone():
        result["ir_material_scan_pages"] = db.execute(
            "SELECT count(*) FROM ir_material_scan_state").fetchone()[0]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cfm_producer_jobs'").fetchone():
        result["cfm_producer_jobs"] = dict(db.execute(
            "SELECT status,count(*) FROM cfm_producer_jobs GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cfm_producer_state'").fetchone():
        row = db.execute("""SELECT last_seed_fetched_at,last_scanned_at,in_scope_items,
            skipped_before_window,last_error FROM cfm_producer_state WHERE source_id='chinaflashmarket'""").fetchone()
        if row:
            result["cfm_producer_state"] = dict(zip(
                ("last_seed_fetched_at", "last_scanned_at", "in_scope_items",
                 "skipped_before_window", "last_error"), row))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nbd_search_state'").fetchone():
        result["nbd_search"] = nbd_search_health(db)
    for table in ("nbd_search_jobs", "nbd_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='twenty_first_search_state'").fetchone():
        result["twenty_first_search"] = twenty_first_search_health(db)
    for table in ("twenty_first_search_jobs", "twenty_first_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jiemian_search_state'").fetchone():
        result["jiemian_search"] = jiemian_search_health(db)
    for table in ("jiemian_search_jobs", "jiemian_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='yicai_search_state'").fetchone():
        result["yicai_search"] = yicai_search_health(db)
    for table in ("yicai_search_jobs", "yicai_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stcn_search_state'").fetchone():
        result["stcn_search"] = stcn_search_health(db)
    for table in ("stcn_search_jobs", "stcn_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cnstock_search_state'").fetchone():
        result["cnstock_search"] = cnstock_search_health(db)
    for table in ("cnstock_search_jobs", "cnstock_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fierce_search_state'").fetchone():
        result["fierce_search"] = {
            source_id: {"company_id": company_id, "search_term": search_term,
                        "last_completed_at": completed, "total_results": total,
                        "pages_scanned": pages, "exact_results": exact,
                        "skipped_before_window": skipped, "last_error": error}
            for source_id, company_id, search_term, completed, total, pages, exact, skipped, error
            in db.execute("""SELECT source_id,company_id,search_term,last_completed_at,
                total_results,pages_scanned,exact_results,skipped_before_window,last_error
                FROM fierce_search_state ORDER BY source_id""")
        }
    for table in ("fierce_search_jobs", "fierce_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='biospace_sitemap_state'").fetchone():
        row = db.execute("""SELECT last_index_fetched_at,last_completed_at,
            coverage_start_month,coverage_end_month,months_scanned,exact_candidates,
            coverage_note,last_error FROM biospace_sitemap_state WHERE source_id='biospace'""").fetchone()
        if row:
            result["biospace_sitemap"] = dict(zip(
                ("last_index_fetched_at", "last_completed_at", "coverage_start_month",
                 "coverage_end_month", "months_scanned", "exact_candidates",
                 "coverage_note", "last_error"), row))
    for table in ("biospace_sitemap_jobs", "biospace_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='toms_sitemap_state'").fetchone():
        row = db.execute("""SELECT last_index_fetched_at,last_completed_at,
            coverage_start_month,coverage_end_month,months_scanned,exact_candidates,
            coverage_note,last_error FROM toms_sitemap_state WHERE source_id='toms_hardware'""").fetchone()
        if row:
            result["toms_sitemap"] = dict(zip(
                ("last_index_fetched_at", "last_completed_at", "coverage_start_month",
                 "coverage_end_month", "months_scanned", "exact_candidates",
                 "coverage_note", "last_error"), row))
    for table in ("toms_sitemap_jobs", "toms_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trendforce_archive_state'").fetchone():
        row = db.execute("""SELECT last_completed_at,pages_scanned,exact_results,
            skipped_before_window,coverage_note,last_error FROM trendforce_archive_state
            WHERE source_id='trendforce'""").fetchone()
        if row:
            result["trendforce_archive"] = dict(zip(
                ("last_completed_at", "pages_scanned", "exact_results",
                 "skipped_before_window", "coverage_note", "last_error"), row))
    for table in ("trendforce_archive_jobs", "trendforce_detail_jobs"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            result[table] = dict(db.execute(
                f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ijiwei_latest_state'").fetchone():
        row = db.execute("""SELECT last_seed_fetched_at,last_scanned_at,visible_items,
            exact_candidates,coverage_note,last_error FROM ijiwei_latest_state
            WHERE source_id='ijiwei'""").fetchone()
        if row:
            result["ijiwei_latest"] = dict(zip(
                ("last_seed_fetched_at", "last_scanned_at", "visible_items",
                 "exact_candidates", "coverage_note", "last_error"), row))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ijiwei_detail_jobs'").fetchone():
        result["ijiwei_detail_jobs"] = dict(db.execute(
            "SELECT status,count(*) FROM ijiwei_detail_jobs GROUP BY status").fetchall())
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_state'").fetchone():
        result["maintenance"] = {
            job_id: {"last_run_at": last_run_at, "status": status}
            for job_id, last_run_at, status in db.execute(
                "SELECT job_id,last_run_at,status FROM maintenance_state ORDER BY job_id")
        }
    db.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Show collector liveness and queue health")
    parser.add_argument("--require-running", action="store_true",
                        help="exit non-zero when no daemon owns the scheduler lock")
    parser.add_argument("--max-stale-minutes", type=int, default=30,
                        help="exit non-zero when the newest audit event is older than this")
    args = parser.parse_args()
    state = snapshot()
    reasons = []
    if args.require_running and not state["daemon_running"]:
        reasons.append("scheduler daemon is not running")
    event = state.get("last_event") or {}
    if event.get("at"):
        age = datetime.now(timezone.utc) - datetime.fromisoformat(event["at"])
        state["last_event_age_seconds"] = max(round(age.total_seconds()), 0)
        if age.total_seconds() > args.max_stale_minutes * 60:
            reasons.append(f"last audit event is older than {args.max_stale_minutes} minutes")
    else:
        reasons.append("no valid audit event found")
    reasons.extend(maintenance_failure_reasons(state.get("maintenance", {})))
    state["healthy"] = not reasons
    state["reasons"] = reasons
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if state["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
