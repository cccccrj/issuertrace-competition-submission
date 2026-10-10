#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collector.collect import ROOT, audit, collect_source, connect, load_sources
from collector.bis_search import process_one as process_one_bis_search_job
from collector.biospace_sitemap import process_one as process_one_biospace_sitemap_job
from collector.cfm_producer import process_one as process_one_cfm_producer_job
from collector.cnstock_search import process_one as process_one_cnstock_search_job
from collector.csrc_search import process_if_due as process_csrc_search_if_due
from collector.federal_register_api import process_one as process_one_federal_register_job
from collector.fierce_search import process_one as process_one_fierce_search_job
from collector.govinfo_packages import process_one as process_one_govinfo_package
from collector.ir_material_access import scan_saved_pages as scan_ir_material_access
from collector.ijiwei_latest import process_one as process_one_ijiwei_latest_job
from collector.jiemian_search import process_one as process_one_jiemian_search_job
from collector.yicai_search import process_one as process_one_yicai_search_job
from collector.local_environment_search import process_one as process_one_environment_search_job
from collector.miit_search import process_one as process_one_miit_search_job
from collector.nbd_search import process_one as process_one_nbd_search_job
from collector.openfda_enforcement import process_one as process_one_openfda_enforcement_job
from collector.report import main as write_report
from collector.sse_api import process_one as process_one_sse_job
from collector.sse_einteractive import process_one as process_one_sse_einteractive_job
from collector.cninfo_api import process_one as process_one_cninfo_job
from collector.sse_ipo_api import process_if_due as process_sse_ipo_if_due
from collector.stcn_search import process_one as process_one_stcn_search_job
from collector.twenty_first_search import process_one as process_one_twenty_first_search_job
from collector.toms_sitemap import process_one as process_one_toms_sitemap_job
from collector.trendforce_archive import process_one as process_one_trendforce_archive_job
from collector.verify_storage import main as verify_storage

CADENCE_SECONDS = {"hourly": 3600, "daily": 86400, "weekly": 604800}
LOCK = ROOT / "data" / "scheduler.lock"

ADAPTER_TABLE_SOURCES = {
    "bis_search_jobs": {"us_bis"},
    "openfda_enforcement_jobs": {"us_fda_openfda"},
    "govinfo_package_jobs": {"us_govinfo"},
    "sse_einteractive_jobs": {"sse_einteractive"},
    "local_environment_search_jobs": {"local_environment"},
    "miit_search_jobs": {"miit"},
    "cfm_producer_jobs": {"chinaflashmarket"},
    "nbd_search_jobs": {"nbd"}, "nbd_detail_jobs": {"nbd"},
    "twenty_first_search_jobs": {"twenty_first_finance"},
    "twenty_first_detail_jobs": {"twenty_first_finance"},
    "fierce_search_jobs": {"fierce_biotech", "fierce_pharma"},
    "fierce_detail_jobs": {"fierce_biotech", "fierce_pharma"},
    "biospace_sitemap_jobs": {"biospace"}, "biospace_detail_jobs": {"biospace"},
    "toms_sitemap_jobs": {"toms_hardware"}, "toms_detail_jobs": {"toms_hardware"},
    "trendforce_archive_jobs": {"trendforce"}, "trendforce_detail_jobs": {"trendforce"},
    "jiemian_search_jobs": {"jiemian"}, "jiemian_detail_jobs": {"jiemian"},
    "yicai_search_jobs": {"yicai"}, "yicai_detail_jobs": {"yicai"},
    "stcn_search_jobs": {"securities_times"}, "stcn_detail_jobs": {"securities_times"},
    "cnstock_search_jobs": {"shanghai_securities_news"},
    "cnstock_detail_jobs": {"shanghai_securities_news"},
}


def scheduling_scope(config: dict, sources) -> tuple[dict, set[str]]:
    settings = config.get("collection_defaults", {}).get("scheduling", {}) or {}
    configured = settings.get("active_source_ids") or []
    active = set(configured) if configured else {source.source_id for source in sources}
    return settings, active


def campaign_completion_progress(db: sqlite3.Connection, settings: dict) -> tuple[int, int] | None:
    gate = settings.get("completion_gate") or {}
    company_id = gate.get("company_id")
    target = gate.get("minimum_valid_documents")
    document_types = list(gate.get("document_types") or [])
    if not company_id or not target or not document_types:
        return None
    placeholders = ",".join("?" for _ in document_types)
    current = db.execute(f"""SELECT count(*) FROM documents d
        JOIN document_versions v ON v.source_id=d.source_id
          AND v.canonical_url=d.canonical_url
        WHERE v.version_id=(SELECT v2.version_id FROM document_versions v2
            WHERE v2.source_id=d.source_id AND v2.canonical_url=d.canonical_url
            ORDER BY v2.fetched_at DESC,v2.rowid DESC LIMIT 1)
          AND v.company_id=? AND v.document_type IN ({placeholders})
          AND COALESCE(v.review_status,'') NOT IN
            ('no_entity_match','ambiguous_entity_match','outside_backfill_window',
             'redirected_to_landing','search_response','pending_entity_review',
             'manual_import_pending_review','manual_import_rejected','source_landing')""",
                         (company_id, *document_types)).fetchone()[0]
    return int(current), int(target)


def campaign_should_stop(config: dict) -> tuple[bool, int, int]:
    settings = config.get("collection_defaults", {}).get("scheduling", {}) or {}
    db = connect()
    try:
        progress = campaign_completion_progress(db, settings)
    finally:
        db.close()
    if not progress:
        return False, 0, 0
    current, target = progress
    return bool(settings.get("completion_gate", {}).get("stop_daemon") and current >= target), current, target


def due(db: sqlite3.Connection, source) -> bool:
    row = db.execute("SELECT last_checked_at,cooldown_until,state,consecutive_failures FROM source_state WHERE source_id=?", (source.source_id,)).fetchone()
    if not row:
        return True
    last_checked, cooldown, state, failures = row
    now = datetime.now(timezone.utc)
    if state == "manual_review":
        return False
    if cooldown and datetime.fromisoformat(cooldown) > now:
        return False
    pending = db.execute("SELECT 1 FROM crawl_frontier WHERE source_id=? AND status='pending' LIMIT 1", (source.source_id,)).fetchone()
    if pending:
        return True
    if failures:
        return True  # cooldown expired: retry/half-open probe before normal cadence
    interval = CADENCE_SECONDS.get(source.raw.get("cadence", "daily"), 86400)
    return datetime.fromisoformat(last_checked) + timedelta(seconds=interval) <= now


def last_checked_key(db: sqlite3.Connection, source) -> str:
    row = db.execute("SELECT last_checked_at FROM source_state WHERE source_id=?", (source.source_id,)).fetchone()
    return row[0] if row and row[0] else "0000-00-00T00:00:00+00:00"


def has_pending_sse_backfill(db: sqlite3.Connection) -> bool:
    """Return whether historical SSE slices remain, without counting live increments."""
    table = db.execute("""SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='api_jobs'""").fetchone()
    if not table:
        return False
    return db.execute("""SELECT 1 FROM api_jobs
        WHERE source_id='sse_announcements' AND status='pending'
          AND job_id NOT LIKE 'incremental:%' LIMIT 1""").fetchone() is not None


def has_runnable_frontier(db: sqlite3.Connection, source_ids: list[str]) -> bool:
    if not source_ids:
        return False
    placeholders = ",".join("?" for _ in source_ids)
    row = db.execute(f"""SELECT 1 FROM crawl_frontier f
        LEFT JOIN source_state s ON s.source_id=f.source_id
        WHERE f.status='pending' AND f.source_id IN ({placeholders})
          AND COALESCE(s.state,'closed')!='manual_review'
          AND (s.cooldown_until IS NULL OR s.cooldown_until<=?) LIMIT 1""",
                     (*source_ids, datetime.now(timezone.utc).isoformat(timespec="seconds"))).fetchone()
    return row is not None


def has_pending_adapter_backfill(db: sqlite3.Connection, priority: str,
                                 active_source_ids: set[str] | None = None) -> bool:
    tables = []
    if priority in {"P0", "ALL"}:
        tables.extend(("bis_search_jobs", "openfda_enforcement_jobs",
                       "govinfo_package_jobs"))
    if priority in {"P1", "ALL"}:
        tables.extend(("sse_einteractive_jobs", "local_environment_search_jobs",
                       "miit_search_jobs", "cfm_producer_jobs", "nbd_search_jobs",
                       "nbd_detail_jobs", "twenty_first_search_jobs",
                       "twenty_first_detail_jobs", "fierce_search_jobs",
                       "fierce_detail_jobs", "biospace_sitemap_jobs",
                       "biospace_detail_jobs", "toms_sitemap_jobs",
                       "toms_detail_jobs", "trendforce_archive_jobs",
                       "trendforce_detail_jobs"))
        tables.extend(("jiemian_search_jobs", "jiemian_detail_jobs"))
        tables.extend(("yicai_search_jobs", "yicai_detail_jobs"))
        tables.extend(("stcn_search_jobs", "stcn_detail_jobs"))
        tables.extend(("cnstock_search_jobs", "cnstock_detail_jobs"))
    for table in tables:
        if (active_source_ids is not None and
                not (ADAPTER_TABLE_SOURCES.get(table, set()) & active_source_ids)):
            continue
        exists = db.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name=?""", (table,)).fetchone()
        if exists and db.execute(f"""SELECT 1 FROM {table}
                WHERE mode='backfill' AND status='pending' LIMIT 1""").fetchone():
            return True
    return False


def sleep_base_seconds(priority: str, regular: int, catch_up: int, pending_backfill: bool,
                       pending_frontier: bool = False) -> int:
    if (priority in {"P0", "ALL"} and pending_backfill) or pending_frontier:
        return catch_up
    return regular


def maintenance_due(db: sqlite3.Connection, job_id: str, interval_hours: int,
                    now: datetime | None = None) -> bool:
    if interval_hours <= 0:
        return False
    row = db.execute("SELECT last_run_at FROM maintenance_state WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return True
    current = now or datetime.now(timezone.utc)
    return datetime.fromisoformat(row[0]) + timedelta(hours=interval_hours) <= current


def run_batch(priority: str, max_sources: int, max_pages: int, contact: str, discover: bool,
              sse_jobs_per_cycle: int = 1, verify_storage_interval_hours: int = 24) -> tuple[int, int]:
    configured_priority = None if priority == "ALL" else priority
    config, sources = load_sources(ROOT / "config" / "sources.yaml", set(), configured_priority)
    settings, active_source_ids = scheduling_scope(config, sources)
    enabled = active_source_ids.__contains__
    db = connect()
    due_sources = [source for source in sources if enabled(source.source_id)
                   and not source.raw.get("adapter") and due(db, source)]
    selected = sorted(due_sources, key=lambda source: (last_checked_key(db, source), source.source_id))[:max_sources]
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    for source in selected:
        source_discover = discover and (
            source.raw.get("collection_mode") == "crawl" or
            source.raw.get("discover_search_results", False))
        collect_source(db, source, user_agent, max_pages, source_discover)
    db.close()
    adapter_jobs_processed = 0
    if priority in {"P0", "ALL"}:
        if enabled("sse_announcements"):
            for job_index in range(sse_jobs_per_cycle):
                processed = process_one_sse_job(contact)
                adapter_jobs_processed += processed
                if processed <= 0:
                    break
                if job_index + 1 < sse_jobs_per_cycle:
                    time.sleep(10)
        if enabled("cninfo_announcements"):
            for job_index in range(sse_jobs_per_cycle):
                processed = process_one_cninfo_job(contact)
                adapter_jobs_processed += processed
                if processed <= 0:
                    break
                if job_index + 1 < sse_jobs_per_cycle:
                    time.sleep(10)
        if enabled("sse_ipo_review"):
            adapter_jobs_processed += process_sse_ipo_if_due(contact)
        if enabled("us_federal_register_api"):
            adapter_jobs_processed += process_one_federal_register_job(contact)
        if enabled("csrc"):
            adapter_jobs_processed += process_csrc_search_if_due(contact)
        if enabled("us_bis"):
            adapter_jobs_processed += process_one_bis_search_job(contact)
        if enabled("us_fda_openfda"):
            adapter_jobs_processed += process_one_openfda_enforcement_job(contact)
        if enabled("us_govinfo"):
            adapter_jobs_processed += process_one_govinfo_package(contact)
        if enabled("wuxi_ir_materials"):
            adapter_jobs_processed += scan_ir_material_access()
    if priority in {"P1", "ALL"}:
        adapter_jobs_per_source = int(settings.get("adapter_jobs_per_source_per_cycle", 1))
        p1_jobs = (
            ({"sse_einteractive"}, process_one_sse_einteractive_job),
            ({"local_environment"}, process_one_environment_search_job),
            ({"miit"}, process_one_miit_search_job),
            ({"chinaflashmarket"}, process_one_cfm_producer_job),
            ({"nbd"}, process_one_nbd_search_job),
            ({"twenty_first_finance"}, process_one_twenty_first_search_job),
            ({"fierce_biotech", "fierce_pharma"}, process_one_fierce_search_job),
            ({"biospace"}, process_one_biospace_sitemap_job),
            ({"toms_hardware"}, process_one_toms_sitemap_job),
            ({"trendforce"}, process_one_trendforce_archive_job),
            ({"ijiwei"}, process_one_ijiwei_latest_job),
            ({"jiemian"}, process_one_jiemian_search_job),
            ({"yicai"}, process_one_yicai_search_job),
            ({"securities_times"}, process_one_stcn_search_job),
            ({"shanghai_securities_news"}, process_one_cnstock_search_job),
        )
        for source_ids, process in p1_jobs:
            if source_ids & active_source_ids:
                for _ in range(adapter_jobs_per_source):
                    processed = process(contact)
                    adapter_jobs_processed += processed
                    if processed <= 0:
                        break
    maintenance_db = connect()
    try:
        verify_due = maintenance_due(
            maintenance_db, "verify_storage", verify_storage_interval_hours)
    finally:
        maintenance_db.close()
    if verify_due:
        verify_storage()
    audit({"source_id": "system", "outcome": "scheduler_cycle", "priority": priority,
           "selected_sources": [source.source_id for source in selected],
           "selected_count": len(selected), "max_pages": max_pages, "discover": discover,
           "campaign_id": settings.get("campaign_id"),
           "active_source_ids": sorted(active_source_ids),
           "sse_jobs_per_cycle": sse_jobs_per_cycle,
           "adapter_jobs_processed": adapter_jobs_processed})
    write_report()
    return len(selected), adapter_jobs_processed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cadence-aware, non-overlapping collection scheduler")
    p.add_argument("--priority", choices=["P0", "P1", "P2", "ALL"], default="P0",
                   help="priority to run; ALL fairly rotates every automatic source")
    p.add_argument("--max-sources", type=int, default=4, help="source cap per scheduling cycle")
    p.add_argument("--max-pages", type=int, default=1, help="page cap per source")
    p.add_argument("--daemon", action="store_true", help="keep checking for due work")
    p.add_argument("--discover", action="store_true", help="persist and drain same-domain public archive links")
    p.add_argument("--interval-seconds", type=int, default=300)
    p.add_argument("--catch-up-interval-seconds", type=int, default=60,
                   help="cycle interval while runnable historical/frontier work remains (default: 60)")
    p.add_argument("--sse-jobs-per-cycle", type=int, default=1,
                   help="SSE monthly jobs per cycle, separated by at least 10 seconds (default: 1)")
    p.add_argument("--verify-storage-interval-hours", type=int, default=24,
                   help="offline SHA-256 verification interval; 0 disables it (default: 24)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.max_sources <= 20 or not 1 <= args.max_pages <= 20:
        raise SystemExit("source/page caps must be between 1 and 20")
    if args.interval_seconds < 60 or args.catch_up_interval_seconds < 60:
        raise SystemExit("cycle intervals must be at least 60 seconds")
    if args.catch_up_interval_seconds > args.interval_seconds:
        raise SystemExit("--catch-up-interval-seconds cannot exceed --interval-seconds")
    if not 1 <= args.sse_jobs_per_cycle <= 6:
        raise SystemExit("--sse-jobs-per-cycle must be between 1 and 6")
    if args.verify_storage_interval_hours < 0:
        raise SystemExit("--verify-storage-interval-hours cannot be negative")
    startup_config, startup_sources = load_sources(
        ROOT / "config" / "sources.yaml", set(), None)
    startup_settings, _ = scheduling_scope(startup_config, startup_sources)
    if startup_settings.get("paused", False):
        print(f"scheduler paused by config campaign={startup_settings.get('campaign_id', 'unnamed')}")
        return 0
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required for scheduled collection")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("scheduler already running", file=sys.stderr)
            return 0
        while True:
            current_config, _ = load_sources(
                ROOT / "config" / "sources.yaml", set(), None)
            should_stop, current_documents, target_documents = campaign_should_stop(current_config)
            if should_stop:
                audit({"source_id": "system", "outcome": "campaign_completion_gate_reached",
                       "campaign_id": startup_settings.get("campaign_id"),
                       "current_documents": current_documents,
                       "target_documents": target_documents})
                write_report()
                print(f"campaign_completion_gate_reached current={current_documents} "
                      f"target={target_documents}", flush=True)
                return 0
            try:
                source_count, adapter_jobs = run_batch(
                    args.priority, args.max_sources, args.max_pages, contact, args.discover,
                    args.sse_jobs_per_cycle, args.verify_storage_interval_hours)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                source_count = 0
                adapter_jobs = 0
                audit({"source_id": "system", "outcome": "scheduler_cycle_deferred_db_busy",
                       "error": str(exc)[:300]})
                print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                      f"collected_sources=0 deferred_database_busy=true", flush=True)
            print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                  f"source_tasks={source_count} adapter_jobs={adapter_jobs}", flush=True)
            current_config, _ = load_sources(
                ROOT / "config" / "sources.yaml", set(), None)
            should_stop, current_documents, target_documents = campaign_should_stop(current_config)
            if should_stop:
                audit({"source_id": "system", "outcome": "campaign_completion_gate_reached",
                       "campaign_id": startup_settings.get("campaign_id"),
                       "current_documents": current_documents,
                       "target_documents": target_documents})
                write_report()
                print(f"campaign_completion_gate_reached current={current_documents} "
                      f"target={target_documents}", flush=True)
                return 0
            if not args.daemon:
                return 0
            db = connect()
            try:
                pending_backfill = has_pending_sse_backfill(db)
                configured_priority = None if args.priority == "ALL" else args.priority
                interval_config, interval_sources = load_sources(
                    ROOT / "config" / "sources.yaml", set(), configured_priority)
                _, interval_active = scheduling_scope(interval_config, interval_sources)
                pending_frontier = has_runnable_frontier(
                    db, [source.source_id for source in interval_sources
                         if source.source_id in interval_active and not source.raw.get("adapter")])
                pending_frontier = pending_frontier or has_pending_adapter_backfill(
                    db, args.priority, interval_active)
            finally:
                db.close()
            sleep_base = sleep_base_seconds(
                args.priority, args.interval_seconds, args.catch_up_interval_seconds,
                pending_backfill, pending_frontier)
            print(f"next_cycle_base_seconds={sleep_base} pending_sse_backfill={pending_backfill} "
                  f"pending_frontier={pending_frontier}", flush=True)
            time.sleep(sleep_base * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    sys.exit(main())
