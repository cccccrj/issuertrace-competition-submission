#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "logs" / "collection.jsonl"
DB = ROOT / "data" / "state" / "collector.sqlite3"
REPORT = ROOT / "docs" / "采集运行台账.md"
CONFIG = ROOT / "config" / "sources.yaml"


def clean(value: object) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def main() -> None:
    events = []
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    outcomes = Counter(e.get("outcome", "unknown") for e in events)
    storage_verification = next((e for e in reversed(events)
        if e.get("outcome") in {"storage_verification_success", "storage_verification_error"}), None)
    documents = []
    states = []
    versions = 0
    attachments = 0
    manual_import_provenance = []
    attachment_metadata_missing = {"title": 0, "published_at": 0, "company_id": 0, "document_type": 0}
    manual_urls = 0
    frontier = []
    api_jobs = []
    federal_register_jobs = []
    csrc_search_jobs = []
    einteractive_state = []
    einteractive_jobs = []
    environment_search_state = []
    environment_search_jobs = []
    miit_search_state = []
    miit_search_jobs = []
    bis_search_state = []
    bis_search_jobs = []
    openfda_enforcement_state = []
    openfda_enforcement_jobs = []
    govinfo_package_jobs = []
    ir_material_access_jobs = []
    cfm_producer_jobs = []
    cfm_producer_state = []
    nbd_search_state = []
    nbd_search_jobs = []
    nbd_detail_jobs = []
    twenty_first_search_state = []
    twenty_first_search_jobs = []
    twenty_first_detail_jobs = []
    jiemian_search_state = []
    jiemian_search_jobs = []
    jiemian_detail_jobs = []
    yicai_search_state = []
    yicai_search_jobs = []
    yicai_detail_jobs = []
    stcn_search_state = []
    stcn_search_jobs = []
    stcn_detail_jobs = []
    cnstock_search_state = []
    cnstock_search_jobs = []
    cnstock_detail_jobs = []
    fierce_search_state = []
    fierce_search_jobs = []
    fierce_detail_jobs = []
    biospace_sitemap_state = []
    biospace_sitemap_jobs = []
    biospace_detail_jobs = []
    toms_sitemap_state = []
    toms_sitemap_jobs = []
    toms_detail_jobs = []
    trendforce_archive_state = []
    trendforce_archive_jobs = []
    trendforce_detail_jobs = []
    ijiwei_latest_state = []
    ijiwei_detail_jobs = []
    ipo_sync = []
    ipo_projects = []
    host_states = []
    source_coverage = []
    manual_frontier_rows = []
    documents_without_version = 0
    missing_raw_files = 0
    entity_document_counts: dict[str, int] = {}
    entity_document_type_counts = []
    focus_company = None
    focus_company_label = "未设置（主体间公平轮转）"
    campaign_id = "—"
    campaign_paused = False
    active_source_ids: list[str] = []
    if DB.exists():
        db = sqlite3.connect(DB)
        documents = db.execute("SELECT source_id,canonical_url,fetched_at,status,content_hash,raw_path,review_status FROM documents ORDER BY fetched_at DESC").fetchall()
        documents_without_version = db.execute("""SELECT count(*) FROM documents d
            LEFT JOIN document_versions v ON v.source_id=d.source_id AND v.canonical_url=d.canonical_url
            WHERE v.version_id IS NULL""").fetchone()[0]
        entity_document_counts = dict(db.execute("""SELECT v.company_id,count(*)
            FROM documents d JOIN document_versions v
              ON v.source_id=d.source_id AND v.canonical_url=d.canonical_url
            WHERE v.version_id=(SELECT v2.version_id FROM document_versions v2
                WHERE v2.source_id=d.source_id AND v2.canonical_url=d.canonical_url
                ORDER BY v2.fetched_at DESC,v2.rowid DESC LIMIT 1)
              AND COALESCE(v.company_id,'')<>''
              AND COALESCE(v.review_status,'') NOT IN
                ('no_entity_match','ambiguous_entity_match','outside_backfill_window',
                 'redirected_to_landing','search_response','pending_entity_review',
                 'manual_import_pending_review','manual_import_rejected','source_landing')
              AND v.document_type NOT IN ('landing_page','search_response')
            GROUP BY v.company_id""").fetchall())
        # Keep this definition identical to the headline's valid-entity count:
        # one current version per URL, with search/landing and unresolved
        # records excluded.  The type table is therefore a reconciliation aid,
        # rather than a count of every raw response or every version.
        entity_document_type_counts = db.execute("""SELECT v.company_id,
            COALESCE(NULLIF(TRIM(v.document_type),''),'未分类') AS document_type,
            count(*) AS url_count
            FROM documents d JOIN document_versions v
              ON v.source_id=d.source_id AND v.canonical_url=d.canonical_url
            WHERE v.version_id=(SELECT v2.version_id FROM document_versions v2
                WHERE v2.source_id=d.source_id AND v2.canonical_url=d.canonical_url
                ORDER BY v2.fetched_at DESC,v2.rowid DESC LIMIT 1)
              AND COALESCE(v.company_id,'')<>''
              AND COALESCE(v.review_status,'') NOT IN
                ('no_entity_match','ambiguous_entity_match','outside_backfill_window',
                 'redirected_to_landing','search_response','pending_entity_review',
                 'manual_import_pending_review','manual_import_rejected','source_landing')
              AND v.document_type NOT IN ('landing_page','search_response')
            GROUP BY v.company_id,document_type
            ORDER BY v.company_id, url_count DESC, document_type""").fetchall()
        missing_raw_files = sum(1 for row in documents if not row[5] or not (ROOT / row[5]).exists())
        states = db.execute("SELECT source_id,last_checked_at,state,cooldown_until,last_error FROM source_state ORDER BY source_id").fetchall()
        versions = db.execute("SELECT count(*) FROM document_versions").fetchone()[0]
        attachments = db.execute("SELECT count(*) FROM document_versions WHERE attachment_hash IS NOT NULL").fetchone()[0]
        if db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='manual_import_provenance'""").fetchone():
            manual_import_provenance = db.execute("""SELECT p.review_status,
                p.acquisition_source_tier,p.equivalence_status,count(*)
                FROM manual_import_provenance p
                GROUP BY p.review_status,p.acquisition_source_tier,p.equivalence_status
                ORDER BY p.review_status,p.acquisition_source_tier,p.equivalence_status""").fetchall()
        missing_row = db.execute("""SELECT
            SUM(CASE WHEN COALESCE(TRIM(title),'')='' THEN 1 ELSE 0 END),
            SUM(CASE WHEN COALESCE(TRIM(published_at),'')='' THEN 1 ELSE 0 END),
            SUM(CASE WHEN COALESCE(TRIM(company_id),'')='' THEN 1 ELSE 0 END),
            SUM(CASE WHEN COALESCE(TRIM(document_type),'')='' THEN 1 ELSE 0 END)
            FROM document_versions WHERE attachment_hash IS NOT NULL""").fetchone()
        attachment_metadata_missing = dict(zip(
            ("title", "published_at", "company_id", "document_type"),
            (int(value or 0) for value in missing_row)))
        manual_urls = db.execute("SELECT count(*) FROM crawl_frontier WHERE status='manual_review'").fetchone()[0]
        manual_frontier_rows = db.execute("""SELECT source_id,url,discovered_from,discovered_at,last_error
            FROM crawl_frontier WHERE status='manual_review'
            ORDER BY discovered_at DESC,source_id,url LIMIT 200""").fetchall()
        frontier = db.execute("SELECT source_id,status,count(*) FROM crawl_frontier GROUP BY source_id,status ORDER BY source_id,status").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_jobs'").fetchone():
            api_jobs = db.execute("""SELECT CASE WHEN job_id LIKE 'incremental:%'
                THEN 'incremental' ELSE 'backfill' END AS job_class,status,count(*)
                FROM api_jobs GROUP BY job_class,status ORDER BY job_class,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federal_register_jobs'").fetchone():
            federal_register_jobs = db.execute("SELECT status,count(*) FROM federal_register_jobs GROUP BY status ORDER BY status").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='csrc_search_jobs'").fetchone():
            csrc_search_jobs = db.execute("""SELECT company_id,search_term,status,attempts,last_run_at,last_error
                FROM csrc_search_jobs ORDER BY company_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sse_einteractive_state'").fetchone():
            einteractive_state = db.execute("""SELECT s.company_id,s.stock_code,s.uid,
                s.backfill_coverage,s.coverage_start_at,s.last_incremental_date,
                count(i.item_id),s.last_error FROM sse_einteractive_state s
                LEFT JOIN sse_einteractive_items i ON i.company_id=s.company_id
                GROUP BY s.company_id,s.stock_code,s.uid,s.backfill_coverage,
                    s.coverage_start_at,s.last_incremental_date,s.last_error
                ORDER BY s.company_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sse_einteractive_jobs'").fetchone():
            einteractive_jobs = db.execute("""SELECT mode,status,count(*)
                FROM sse_einteractive_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_environment_search_state'").fetchone():
            environment_search_state = db.execute("""SELECT s.term_id,s.company_id,s.search_term,
                s.last_completed_at,(SELECT count(*) FROM crawl_frontier f
                    WHERE f.source_id='local_environment' AND
                    f.discovered_from='local_environment_search:' || s.term_id),s.last_error
                FROM local_environment_search_state s ORDER BY s.term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_environment_search_jobs'").fetchone():
            environment_search_jobs = db.execute("""SELECT mode,status,count(*)
                FROM local_environment_search_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='miit_search_state'").fetchone():
            miit_search_state = db.execute("""SELECT s.term_id,s.search_term,s.last_completed_at,
                s.total_results,(SELECT count(*) FROM crawl_frontier f
                    WHERE f.source_id='miit' AND f.discovered_from='miit_search:' || s.term_id),
                s.last_error FROM miit_search_state s ORDER BY s.term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='miit_search_jobs'").fetchone():
            miit_search_jobs = db.execute("""SELECT mode,status,count(*) FROM miit_search_jobs
                GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bis_search_state'").fetchone():
            bis_search_state = db.execute("""SELECT s.term_id,s.company_id,s.search_term,
                s.last_completed_at,s.total_results,(SELECT count(*) FROM crawl_frontier f
                    WHERE f.source_id='us_bis' AND
                    f.discovered_from='bis_search:' || s.term_id),s.last_error
                FROM bis_search_state s ORDER BY s.term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bis_search_jobs'").fetchone():
            bis_search_jobs = db.execute("""SELECT mode,status,count(*) FROM bis_search_jobs
                GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='openfda_enforcement_state'").fetchone():
            openfda_enforcement_state = db.execute("""SELECT s.term_id,s.endpoint_type,
                s.company_id,s.search_term,s.last_completed_at,s.total_results,
                (SELECT count(*) FROM openfda_enforcement_items i
                    WHERE i.term_id=s.term_id),s.last_error
                FROM openfda_enforcement_state s ORDER BY s.term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='openfda_enforcement_jobs'").fetchone():
            openfda_enforcement_jobs = db.execute("""SELECT mode,status,count(*)
                FROM openfda_enforcement_jobs GROUP BY mode,status
                ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='govinfo_package_jobs'").fetchone():
            govinfo_package_jobs = db.execute("""SELECT package_id,document_type,
                published_at,status,attempts,last_error FROM govinfo_package_jobs
                ORDER BY published_at,package_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ir_material_access_jobs'").fetchone():
            ir_material_access_jobs = db.execute("""SELECT published_at,title,material_label,
                status,source_url,public_url,reason FROM ir_material_access_jobs
                ORDER BY published_at DESC,title,material_label""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cfm_producer_jobs'").fetchone():
            cfm_producer_jobs = db.execute("""SELECT published_at,title,document_type,mode,
                status,attempts,last_error,url FROM cfm_producer_jobs
                ORDER BY published_at DESC,url""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cfm_producer_state'").fetchone():
            cfm_producer_state = db.execute("""SELECT last_seed_fetched_at,last_scanned_at,
                in_scope_items,skipped_before_window,last_error FROM cfm_producer_state
                WHERE source_id='chinaflashmarket'""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nbd_search_state'").fetchone():
            nbd_search_state = db.execute("""SELECT term_id,company_id,search_term,
                last_completed_at,total_results,exact_results,last_error
                FROM nbd_search_state ORDER BY term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nbd_search_jobs'").fetchone():
            nbd_search_jobs = db.execute("""SELECT mode,status,count(*) FROM nbd_search_jobs
                GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nbd_detail_jobs'").fetchone():
            nbd_detail_jobs = db.execute("""SELECT mode,status,count(*) FROM nbd_detail_jobs
                GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='twenty_first_search_state'").fetchone():
            twenty_first_search_state = db.execute("""SELECT term_id,company_id,search_term,
                last_completed_at,pages_scanned,exact_results,skipped_before_window,last_error
                FROM twenty_first_search_state ORDER BY term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='twenty_first_search_jobs'").fetchone():
            twenty_first_search_jobs = db.execute("""SELECT mode,status,count(*)
                FROM twenty_first_search_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='twenty_first_detail_jobs'").fetchone():
            twenty_first_detail_jobs = db.execute("""SELECT mode,status,count(*)
                FROM twenty_first_detail_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jiemian_search_state'").fetchone():
            jiemian_search_state = db.execute("""SELECT term_id,company_id,search_term,
                last_completed_at,pages_scanned,exact_results,skipped_before_window,
                coverage_note,last_error FROM jiemian_search_state ORDER BY term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jiemian_search_jobs'").fetchone():
            jiemian_search_jobs = db.execute("""SELECT mode,status,count(*)
                FROM jiemian_search_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jiemian_detail_jobs'").fetchone():
            jiemian_detail_jobs = db.execute("""SELECT mode,status,count(*)
                FROM jiemian_detail_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='yicai_search_state'").fetchone():
            yicai_search_state = db.execute("""SELECT term_id,company_id,search_term,
                last_completed_at,pages_scanned,exact_results,skipped_before_window,
                coverage_note,last_error FROM yicai_search_state ORDER BY term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='yicai_search_jobs'").fetchone():
            yicai_search_jobs = db.execute("""SELECT mode,status,count(*)
                FROM yicai_search_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='yicai_detail_jobs'").fetchone():
            yicai_detail_jobs = db.execute("""SELECT mode,status,count(*)
                FROM yicai_detail_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stcn_search_state'").fetchone():
            stcn_search_state = db.execute("""SELECT term_id,company_id,search_term,
                last_completed_at,pages_scanned,exact_results,skipped_before_window,
                coverage_note,last_error FROM stcn_search_state ORDER BY term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stcn_search_jobs'").fetchone():
            stcn_search_jobs = db.execute("""SELECT mode,status,count(*) FROM stcn_search_jobs
                GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stcn_detail_jobs'").fetchone():
            stcn_detail_jobs = db.execute("""SELECT mode,status,count(*) FROM stcn_detail_jobs
                GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cnstock_search_state'").fetchone():
            cnstock_search_state = db.execute("""SELECT term_id,company_id,search_term,
                last_completed_at,total_results,pages_scanned,exact_results,
                skipped_before_window,coverage_note,last_error
                FROM cnstock_search_state ORDER BY term_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cnstock_search_jobs'").fetchone():
            cnstock_search_jobs = db.execute("""SELECT mode,status,count(*) FROM cnstock_search_jobs
                GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cnstock_detail_jobs'").fetchone():
            cnstock_detail_jobs = db.execute("""SELECT mode,status,count(*) FROM cnstock_detail_jobs
                GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fierce_search_state'").fetchone():
            fierce_search_state = db.execute("""SELECT source_id,search_term,last_completed_at,
                total_results,pages_scanned,exact_results,skipped_before_window,last_error
                FROM fierce_search_state ORDER BY source_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fierce_search_jobs'").fetchone():
            fierce_search_jobs = db.execute("""SELECT source_id,mode,status,count(*)
                FROM fierce_search_jobs GROUP BY source_id,mode,status
                ORDER BY source_id,mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fierce_detail_jobs'").fetchone():
            fierce_detail_jobs = db.execute("""SELECT source_id,mode,status,count(*)
                FROM fierce_detail_jobs GROUP BY source_id,mode,status
                ORDER BY source_id,mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='biospace_sitemap_state'").fetchone():
            biospace_sitemap_state = db.execute("""SELECT last_index_fetched_at,
                last_completed_at,coverage_start_month,coverage_end_month,months_scanned,
                exact_candidates,coverage_note,last_error FROM biospace_sitemap_state
                WHERE source_id='biospace'""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='biospace_sitemap_jobs'").fetchone():
            biospace_sitemap_jobs = db.execute("""SELECT mode,status,count(*)
                FROM biospace_sitemap_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='biospace_detail_jobs'").fetchone():
            biospace_detail_jobs = db.execute("""SELECT mode,status,count(*)
                FROM biospace_detail_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='toms_sitemap_state'").fetchone():
            toms_sitemap_state = db.execute("""SELECT last_index_fetched_at,
                last_completed_at,coverage_start_month,coverage_end_month,months_scanned,
                exact_candidates,coverage_note,last_error FROM toms_sitemap_state
                WHERE source_id='toms_hardware'""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='toms_sitemap_jobs'").fetchone():
            toms_sitemap_jobs = db.execute("""SELECT mode,status,count(*)
                FROM toms_sitemap_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='toms_detail_jobs'").fetchone():
            toms_detail_jobs = db.execute("""SELECT mode,status,count(*)
                FROM toms_detail_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trendforce_archive_state'").fetchone():
            trendforce_archive_state = db.execute("""SELECT last_completed_at,pages_scanned,
                exact_results,skipped_before_window,coverage_note,last_error
                FROM trendforce_archive_state WHERE source_id='trendforce'""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trendforce_archive_jobs'").fetchone():
            trendforce_archive_jobs = db.execute("""SELECT mode,status,count(*)
                FROM trendforce_archive_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trendforce_detail_jobs'").fetchone():
            trendforce_detail_jobs = db.execute("""SELECT mode,status,count(*)
                FROM trendforce_detail_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ijiwei_latest_state'").fetchone():
            ijiwei_latest_state = db.execute("""SELECT last_seed_fetched_at,last_scanned_at,
                visible_items,exact_candidates,coverage_note,last_error
                FROM ijiwei_latest_state WHERE source_id='ijiwei'""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ijiwei_detail_jobs'").fetchone():
            ijiwei_detail_jobs = db.execute("""SELECT mode,status,count(*)
                FROM ijiwei_detail_jobs GROUP BY mode,status ORDER BY mode,status""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sse_ipo_sync'").fetchone():
            ipo_sync = db.execute("SELECT source_id,last_synced_at,last_error FROM sse_ipo_sync ORDER BY source_id").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sse_ipo_projects'").fetchone():
            ipo_projects = db.execute("""SELECT audit_id,company_name,audit_apply_date,update_date,
                current_status,registration_result FROM sse_ipo_projects ORDER BY audit_id""").fetchall()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_state'").fetchone():
            host_states = db.execute("SELECT source_id,host,state,reason,updated_at FROM host_state ORDER BY source_id,host").fetchall()
        if CONFIG.exists():
            config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
            focus_company = (config.get("collection_defaults", {}).get("scheduling", {})
                             .get("focus_company"))
            scheduling = config.get("collection_defaults", {}).get("scheduling", {}) or {}
            campaign_id = scheduling.get("campaign_id") or "—"
            campaign_paused = bool(scheduling.get("paused", False))
            active_source_ids = list(scheduling.get("active_source_ids") or [])
            entity = (config.get("entities", {}) or {}).get(focus_company, {})
            if focus_company and entity:
                focus_company_label = f"{focus_company}（{entity.get('canonical_name', focus_company)}）"
            state_by_id = {row[0]: row for row in states}
            document_counts = dict(db.execute("SELECT source_id,count(*) FROM documents GROUP BY source_id").fetchall())
            scope_match_counts = dict(db.execute("""SELECT source_id,count(*) FROM documents
                WHERE COALESCE(review_status,'') NOT IN
                    ('no_entity_match','ambiguous_entity_match','outside_backfill_window',
                     'redirected_to_landing','search_response','pending_entity_review',
                     'manual_import_pending_review','manual_import_rejected','source_landing')
                  AND EXISTS (SELECT 1 FROM document_versions v
                    WHERE v.source_id=documents.source_id
                      AND v.canonical_url=documents.canonical_url
                      AND v.version_id=(SELECT v2.version_id FROM document_versions v2
                        WHERE v2.source_id=documents.source_id
                          AND v2.canonical_url=documents.canonical_url
                        ORDER BY v2.fetched_at DESC,v2.rowid DESC LIMIT 1)
                      AND v.document_type NOT IN ('landing_page','search_response'))
                GROUP BY source_id""").fetchall())
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='openfda_enforcement_items'").fetchone():
                scope_match_counts["us_fda_openfda"] = db.execute(
                    "SELECT count(*) FROM openfda_enforcement_items").fetchone()[0]
            for source in config.get("sources", []):
                sid = source["source_id"]
                state_row = state_by_id.get(sid)
                count = document_counts.get(sid, 0)
                matched_count = scope_match_counts.get(sid, 0)
                mode = source.get("collection_mode")
                if mode == "manual":
                    coverage = "manual_collected" if matched_count else "manual_by_policy"
                elif matched_count:
                    coverage = "collected_scope_match"
                elif state_row and state_row[2] == "manual_review":
                    coverage = "manual_review"
                elif state_row and state_row[2] == "cooldown":
                    coverage = "cooldown"
                elif count:
                    coverage = "response_only_no_match"
                elif state_row and state_row[1]:
                    coverage = "attempted_no_document"
                else:
                    coverage = "unattempted"
                source_coverage.append((sid, source.get("priority"), source.get("source_tier"),
                                        mode, coverage, matched_count, count,
                                        state_row[1] if state_row else None,
                                        state_row[4] if state_row else None))
        db.close()
    failure_outcomes = {"transport_error", "http_error", "rate_limited", "manual_review_access_control", "manual_review_challenge", "manual_review_content_mismatch", "robots_wildcard_parser_correction", "api_backfill_error", "api_incremental_error", "ipo_api_sync_error", "official_search_error", "einteractive_company_error", "einteractive_job_error", "environment_search_error", "miit_search_error", "miit_search_suspended_manual_review", "bis_search_error", "bis_search_suspended_manual_review", "openfda_enforcement_error", "openfda_enforcement_suspended_manual_review", "govinfo_package_error", "govinfo_packages_suspended_manual_review", "ir_material_access_manual_review", "cfm_producer_job_error", "cfm_producer_suspended_manual_review", "nbd_search_error", "nbd_detail_error", "nbd_search_suspended_manual_review", "twenty_first_search_error", "twenty_first_detail_error", "twenty_first_search_suspended_manual_review", "jiemian_search_error", "jiemian_detail_error", "jiemian_search_suspended_manual_review", "yicai_search_error", "yicai_detail_error", "yicai_search_suspended_manual_review", "stcn_search_error", "stcn_detail_error", "stcn_search_suspended_manual_review", "cnstock_search_error", "cnstock_detail_error", "cnstock_search_suspended_manual_review", "fierce_search_error", "fierce_detail_error", "fierce_search_suspended_manual_review", "biospace_sitemap_index_error", "biospace_sitemap_month_error", "biospace_detail_error", "biospace_sitemap_suspended_manual_review", "toms_sitemap_index_error", "toms_sitemap_month_error", "toms_detail_error", "toms_sitemap_suspended_manual_review", "trendforce_archive_page_error", "trendforce_detail_error", "trendforce_archive_suspended_manual_review", "ijiwei_latest_seed_error", "ijiwei_detail_error", "ijiwei_latest_suspended_manual_review", "scheduler_cycle_deferred_db_busy", "skipped_host_manual_review", "storage_verification_error"}
    failures = [e for e in events if e.get("outcome") in failure_outcomes or str(e.get("outcome", "")).startswith("robots_error") or e.get("allowed") is False]
    coverage_counts = Counter(row[4] for row in source_coverage)
    raw_covered_sources = sum(1 for row in source_coverage if row[6] > 0)
    matched_sources = sum(1 for row in source_coverage if row[5] > 0)
    configured_sources = len(source_coverage)
    raw_coverage_pct = (raw_covered_sources / configured_sources * 100
                        if configured_sources else 0)
    matched_coverage_pct = (matched_sources / configured_sources * 100
                            if configured_sources else 0)
    lines = [
        "# 数据采集运行台账", "",
        "> 本文档由 `python -m collector.report` 从本地审计日志生成。原文和运行数据库位于被 Git 忽略的 `data/`，不得把受版权保护的全文或代理凭据提交到仓库。", "",
        f"- 最近生成时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 已落盘响应 URL：{len(documents)}", f"- 不可变内容版本：{versions}",
        f"- 有效主体文档（去重 URL、最新版本）：长鑫科技 {entity_document_counts.get('cxmt', 0)}；药明康德 {entity_document_counts.get('wuxi_apptec', 0)}",
        f"- 当前回填优先主体：{focus_company_label}",
        f"- 当前采集活动：{campaign_id}；状态 {'已暂停' if campaign_paused else '运行许可'}；允许调度来源 {', '.join(active_source_ids) if active_source_ids else '全部自动来源'}",
        f"- 已校验 PDF/附件版本：{attachments}",
        f"- 人工导入溯源版本：{sum(row[3] for row in manual_import_provenance)}",
        f"- 待人工处理：URL {manual_urls}；IR 下载窗口材料 {len(ir_material_access_jobs)}",
        f"- 附件元数据空值：标题 {attachment_metadata_missing['title']}；发布日期 {attachment_metadata_missing['published_at']}；主体 {attachment_metadata_missing['company_id']}；文档类型 {attachment_metadata_missing['document_type']}",
        f"- 完整性：缺版本文档 {documents_without_version}；原始文件缺失 {missing_raw_files}",
        (f"- 最近哈希复核：{storage_verification.get('checked_files', 0)} 个文件；缺失 {storage_verification.get('missing_files', 0)}；哈希不符 {storage_verification.get('hash_mismatches', 0)}；"
         f"复核时间 {storage_verification.get('at', '—')}" if storage_verification else "- 最近哈希复核：尚未执行"),
        f"- 来源覆盖：原始响应 {raw_covered_sources} / 配置 {configured_sources}（{raw_coverage_pct:.1f}%）；目标范围命中 {matched_sources} / 配置 {configured_sources}（{matched_coverage_pct:.1f}%）；未尝试 {coverage_counts['unattempted']}；人工策略或限制 {coverage_counts['manual_by_policy'] + coverage_counts['manual_review']}",
        f"- 审计事件：{len(events)}", f"- 失败/人工处理事件：{len(failures)}", "",
        "## 结果汇总", "", "| 结果 | 次数 |", "|---|---:|",
    ]
    lines += [f"| {clean(k)} | {v} |" for k, v in sorted(outcomes.items())]
    lines += ["", "## 有效主体数据类型", "",
              "> 仅统计每个已落盘 URL 的最新版本；检索响应、入口页、未完成主体消歧和窗口外记录不计入。", "",
              "| 主体 | 文档类型 | 去重 URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |"
              for row in entity_document_type_counts]
    lines += ["", "## 回溯队列", "", "| 来源 | 队列状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in frontier]
    lines += ["", "### 上交所公告接口任务", "", "| 任务类型 | 状态 | 分片/分页任务数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in api_jobs]
    lines += ["", "### Federal Register 公开接口任务", "", "| 状态 | 查询/分页任务数 |", "|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in federal_register_jobs]
    lines += ["", "### 中国证监会官网搜索任务", "",
              "| 主体 | 完整法人名查询词 | 状态 | 尝试次数 | 最近运行（UTC） | 最近错误 |",
              "|---|---|---|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in csrc_search_jobs]
    lines += ["", "### 上证 e 互动问答任务", "",
              "> 公开问答接口只返回近期窗口；`partial_recent_window` 和 `no_public_recent_items` 均不代表已覆盖到项目回溯起点。", "",
              "| 主体 | 证券代码 | 公开 UID | 回溯覆盖 | 最早已见问答 | 最近增量日期 | 问答条数 | 最近错误 |",
              "|---|---|---|---|---|---|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in einteractive_state]
    lines += ["", "| 任务类型 | 状态 | 任务数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in einteractive_jobs]
    lines += ["", "### 安徽省生态环境厅精确检索任务", "",
              "> 子公司词命中的记录按主体关联候选保存，须结合招股书或最新合并范围复核后才能作为母公司事实。", "",
              "| 查询 ID | 目标主体 | 精确查询词 | 最近完成（UTC） | 候选详情数 | 最近错误 |",
              "|---|---|---|---|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in environment_search_state]
    lines += ["", "| 任务类型 | 状态 | 分页任务数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in environment_search_jobs]
    lines += ["", "### 工信部官方检索任务", "",
              "| 查询 ID | 精确查询词 | 最近完成（UTC） | 官方结果总数 | 候选详情数 | 最近错误 |",
              "|---|---|---|---:|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in miit_search_state]
    lines += ["", "| 任务类型 | 状态 | 分页任务数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in miit_search_jobs]
    lines += ["", "### 美国商务部 BIS 官网检索任务", "",
              "| 查询 ID | 目标主体 | 精确查询词 | 最近完成（UTC） | 官方结果总数 | 候选详情数 | 最近错误 |",
              "|---|---|---|---|---:|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in bis_search_state]
    lines += ["", "| 任务类型 | 状态 | 查询任务数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in bis_search_jobs]
    lines += ["", "### openFDA 召回执法精确检索任务", "",
              "> 药品与器械端点分开查询；`404 No matches found` 是官方零结果语义并保留原始响应，不代表接口失败。", "",
              "| 查询 ID | 端点 | 目标主体 | 完整短语 | 最近完成（UTC） | 官方结果总数 | 结构化记录数 | 最近错误 |",
              "|---|---|---|---|---|---:|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in openfda_enforcement_state]
    lines += ["", "| 任务类型 | 状态 | 查询任务数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in openfda_enforcement_jobs]
    lines += ["", "### GovInfo 国会立法版本包", "",
              "> 各 PDF 版本独立保全；IH/IS 表示提出，EH 表示众议院通过文本，RFS 表示送交参议院，RS 表示参议院报告文本，均不等同于已生效法律。", "",
              "| 包 ID | 文档类型 | 日期 | 状态 | 尝试次数 | 最近错误 |",
              "|---|---|---|---|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in govinfo_package_jobs]
    lines += ["", "### 药明康德 IR 下载窗口人工任务", "",
              "> 这些标题、日期和材料类型来自已保存的官网页面，但下载控件没有公开 URL，页面要求提交真实信息供审核。自动任务不提交身份信息、不触发控件，也不把可见标题当作已取得附件。", "",
              "| 日期 | 标题 | 材料类型 | 状态 | 来源页面 | 公开下载 URL | 原因 |",
              "|---|---|---|---|---|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in ir_material_access_jobs]
    lines += ["", "### CFM 长鑫厂商页定向任务", "",
              "> 只处理厂商页明确链接且日期不早于 2024-01-01 的资讯和快讯；快讯中的转述、预测和技术参数均为行业线索，不能替代公司或监管原文。", "",
              "| 厂商页最近获取 | 最近解析 | 窗口内链接 | 窗口前跳过 | 最近错误 |",
              "|---|---|---:|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in cfm_producer_state]
    lines += ["", "| 日期 | 标题 | 文档类型 | 任务类型 | 状态 | 尝试 | 最近错误 | URL |",
              "|---|---|---|---|---|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in cfm_producer_jobs]
    lines += ["", "### 每日经济新闻完整法人名检索任务", "",
              "> 只接受结果正文或摘要中的完整法人名精确匹配；查询响应不计作事件，独立报道仍需与公司或监管原文交叉核验。", "",
              "| 查询 ID | 目标主体 | 完整法人名 | 最近完成（UTC） | 官方结果总数 | 窗口内精确候选 | 最近错误 |",
              "|---|---|---|---|---:|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in nbd_search_state]
    lines += ["", "| 检索任务类型 | 状态 | 分页数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in nbd_search_jobs]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in nbd_detail_jobs]
    lines += ["", "### 21财经完整法人名检索任务", "",
              "> 使用公开搜索页声明的密钥和检索接口，严格遵守前端最多 5 页的范围；加密响应按页面公开算法解码。查询响应不计作事件，转载及引述须复核原始出处。", "",
              "| 查询 ID | 目标主体 | 完整法人名 | 最近完成（UTC） | 已扫页数 | 窗口内精确候选 | 窗口前跳过 | 最近错误 |",
              "|---|---|---|---|---:|---:|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in twenty_first_search_state]
    lines += ["", "| 检索任务类型 | 状态 | 分页数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in twenty_first_search_jobs]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in twenty_first_detail_jobs]
    lines += ["", "### 界面新闻公开 HTML 精确检索任务", "",
              "> robots 允许当前检索与 `/article/` 详情路径；按‘长鑫科技’和‘药明康德’强标识低频分页，并对结果标题/摘要再次精确过滤。查询页不计作事件；详情属于独立媒体材料，市场快讯、转载和引述仍须回溯法定公告或原始来源。", "",
              "| 查询 ID | 目标主体 | 强标识 | 最近完成（UTC） | 已扫页数 | 窗口内精确候选 | 窗口前跳过 | 覆盖边界 | 最近错误 |",
              "|---|---|---|---|---:|---:|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in jiemian_search_state]
    lines += ["", "| 检索任务类型 | 状态 | 分页数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in jiemian_search_jobs]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in jiemian_detail_jobs]
    lines += ["", "### 第一财经检索策略状态", "",
              "> 运行时 robots 明确禁止 `/search`，因此自动任务不访问搜索页或其前端结果接口，也不使用替代路径。下表仅保留已停止任务的审计水位；后续材料只能由人工合法导入并复核。", "",
              "| 查询 ID | 目标主体 | 强标识 | 最近完成（UTC） | 已扫页数 | 窗口内精确候选 | 窗口前跳过 | 覆盖边界 | 最近错误 |",
              "|---|---|---|---|---:|---:|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in yicai_search_state]
    lines += ["", "| 检索任务类型 | 状态 | 分页数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in yicai_search_jobs]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in yicai_detail_jobs]
    lines += ["", "### 证券时报匿名公开检索任务", "",
              "> robots.txt 返回 404；任务使用首页公开 GET 搜索和页面声明的同源 JSON 结果端点。匿名 Cookie、Referer 与 X-Requested-With 仅复现普通前端流程，不登录或处理验证码。查询响应不计作事件；行情快讯和转载仍须回溯公告或原始来源。", "",
              "| 查询 ID | 目标主体 | 强标识 | 最近完成（UTC） | 已扫页数 | 窗口内精确候选 | 窗口前跳过 | 覆盖边界 | 最近错误 |",
              "|---|---|---|---|---:|---:|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in stcn_search_state]
    lines += ["", "| 检索任务类型 | 状态 | 分页数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in stcn_search_jobs]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in stcn_detail_jobs]
    lines += ["", "### 上海证券报公开搜索 API 精确检索任务", "",
              "> www 与 api 域 robots.txt 均返回 404；任务只调用官网 searchResult 前端明确声明的公开 JSON POST 端点，使用普通页面请求头，不登录或处理验证码。标题、摘要或正文须再次包含强标识；查询响应不计作事件，普通报道仍须回溯公告或其他原始证据。", "",
              "| 查询 ID | 目标主体 | 强标识 | 最近完成（UTC） | 官方宽匹配总数 | 已扫页数 | 窗口内精确候选 | 窗口前跳过 | 覆盖边界 | 最近错误 |",
              "|---|---|---|---|---:|---:|---:|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in cnstock_search_state]
    lines += ["", "| 检索任务类型 | 状态 | 分页数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in cnstock_search_jobs]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in cnstock_detail_jobs]
    lines += ["", "### Fierce Biotech / Pharma 精确检索任务", "",
              "> 使用公开搜索前端声明的同源 JSON:API，并按站点域过滤；页面按发布日期倒序，每页 10 条，整页进入 2024 年以前即停止。查询响应不计作事件，行业报道中的政策、合同与客户关系仍须回溯原始材料。", "",
              "| 来源 | 完整短语 | 最近完成（UTC） | 官方宽匹配总数 | 已扫页数 | 窗口内精确候选 | 窗口前跳过 | 最近错误 |",
              "|---|---|---|---:|---:|---:|---:|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in fierce_search_state]
    lines += ["", "| 来源 | 检索任务类型 | 状态 | 分页数 |", "|---|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in fierce_search_jobs]
    lines += ["", "| 来源 | 详情任务类型 | 状态 | URL 数 |", "|---|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in fierce_detail_jobs]
    lines += ["", "### BioSpace 月度 sitemap 标题级发现任务", "",
              "> robots 明确禁止 `/search`，自动任务不访问该路径；仅处理 robots 明示的 sitemap 及其 2024-01 起月度索引。候选必须在 URL 标题中完整出现 `WuXi AppTec`，因此该覆盖不包含只在正文中提及主体的文章。企业新闻稿标为企业口径，不能替代独立报道或原始监管材料。", "",
              "| 索引最近获取 | 全量扫描完成 | 覆盖起月 | 覆盖止月 | 已完成月份 | 精确候选 | 覆盖口径 | 最近错误 |",
              "|---|---|---|---|---:|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in biospace_sitemap_state]
    lines += ["", "| sitemap 任务类型 | 状态 | 月份数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in biospace_sitemap_jobs]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in biospace_detail_jobs]
    lines += ["", "### Tom's Hardware 月度 sitemap 标题级发现任务", "",
              "> robots 以通配符明确禁止带 `searchTerm` 的 URL，自动任务不再访问该路径；仅处理 robots 明示的 sitemap 及其 2024-01 起月度索引。候选必须在 URL 标题中出现 `CXMT` 或 `ChangXin Memory Technologies` 强标识。消费级测试、市场估计和报道推断不能写成公司披露事实。", "",
              "| 索引最近获取 | 全量扫描完成 | 覆盖起月 | 覆盖止月 | 已完成月份 | 精确候选 | 覆盖口径 | 最近错误 |",
              "|---|---|---|---|---:|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in toms_sitemap_state]
    lines += ["", "| sitemap 任务类型 | 状态 | 月份数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in toms_sitemap_jobs]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in toms_detail_jobs]
    lines += ["", "### TrendForce 半导体新闻归档精确发现任务", "",
              "> robots 禁止 `/search` 与 `/searchNews`，自动任务不访问这些路径；仅按公开半导体新闻归档倒序分页，标题或摘要须含 `CXMT` / `ChangXin Memory Technologies` 强标识，整页进入 2024 年以前即停止。TrendForce 数字固定标为行业估算，不能写成公司披露值。", "",
              "| 最近完成 | 已扫页数 | 精确候选 | 窗口前跳过 | 覆盖口径 | 最近错误 |",
              "|---|---:|---:|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in trendforce_archive_state]
    lines += ["", "| 归档任务类型 | 状态 | 页数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in trendforce_archive_jobs]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in trendforce_detail_jobs]
    lines += ["", "### 集微网最新 20 条窗口任务", "",
              "> robots 禁止 `/api`，自动任务不访问或逆向该路径；公开 sitemap 仅含导航页，故每天只读取允许的 `/jwnews` SSR 最新窗口。该入口没有历史分页能力，不能据此声称覆盖 2024 年以来全部报道。单独“长鑫”必须同时带 DRAM、LPDDR、存储、芯片或合肥等上下文。", "",
              "| 最新页最近获取 | 最近解析 | 可见条数 | 精确候选 | 覆盖口径 | 最近错误 |",
              "|---|---|---:|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in ijiwei_latest_state]
    lines += ["", "| 详情任务类型 | 状态 | URL 数 |", "|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in ijiwei_detail_jobs]
    lines += ["", "### 上交所发行上市审核同步", "",
              "| 项目号 | 公司 | 受理日期 | 项目更新 | 审核状态代码 | 注册结果代码 |",
              "|---|---|---|---|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in ipo_projects]
    lines += ["", "| 来源 | 最近同步（UTC） | 最近错误 |", "|---|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in ipo_sync]
    lines += ["", "### 域名人工处理状态", "", "| 来源 | 域名 | 状态 | 原因 | 更新时间 |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in host_states]
    lines += ["", "## 来源状态", "", "| 来源 | 最近检查（UTC） | 状态 | 冷却至 | 最近错误 |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in states]
    lines += ["", "## 来源覆盖", "",
              "| 来源 | 优先级 | 证据级 | 模式 | 覆盖状态 | 目标范围命中 URL | 全部落盘 URL | 最近检查（UTC） | 原因 |",
              "|---|---|---|---|---|---:|---:|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in source_coverage]
    lines += ["", "## 待人工 URL（最近 200 条）", "",
              f"> 当前完整人工队列共 {manual_urls} 条；下表按发现时间倒序截取，完整记录保存在 SQLite `crawl_frontier`。", "",
              "| 来源 | URL | 发现自 | 发现时间（UTC） | 原因 |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in manual_frontier_rows]
    lines += ["", "## 已成功保存", "", "| 来源 | URL | 抓取时间 | HTTP | SHA-256 | 原始路径 | 复核状态 |", "|---|---|---|---:|---|---|---|"]
    for row in documents[:200]:
        vals = list(row); vals[4] = str(vals[4])[:12] if vals[4] else "—"
        lines.append("| " + " | ".join(clean(v) for v in vals) + " |")
    lines += ["", "## 失败与人工处理", "", "| 时间 | 来源 | 结果 | HTTP | URL | 原因/冷却 |", "|---|---|---|---:|---|---|"]
    for e in failures[-200:][::-1]:
        reason = e.get("error") or (f"冷却 {e.get('cooldown_seconds')} 秒" if e.get("cooldown_seconds") else "需人工核查")
        row = [e.get("at"), e.get("source_id"), e.get("outcome"), e.get("http_status"), e.get("url"), reason]
        lines.append("| " + " | ".join(clean(v) for v in row) + " |")
    lines += ["", "## 人工导入溯源", "",
              "> 官方参考 URL 与实际取得 URL 分开保存；镜像文件保持其实际证据级，不因引用官方地址自动提升。", "",
              "| 审核状态 | 实际来源级 | 等价性 | 版本数 |", "|---|---|---|---:|"]
    lines += ["| " + " | ".join(clean(v) for v in row) + " |" for row in manual_import_provenance]
    lines += ["", "## 状态解释", "", "- `collected_scope_match`：至少一个落盘响应命中配置主体或来自单一主体官方栏目；不代表内容已形成核实事件。", "- `auto_matched_first_party`：显式登记的单一主体公司官网详情页或附件，可确认发言主体；只代表第一方归属，不独立验证公司主张。", "- `response_only_no_match`：仅保存了入口页、查询响应、未命中主体响应或回溯窗口外内容，不能计作目标数据覆盖。", "- `search_response`：保存了可复查的官方查询响应及水位，但查询词本身不能算作目标事件命中。", "- `redirected_to_landing`：旧深层链接被站点重定向到来源入口页，保留审计但不计作有效栏目内容。", "- `attempted_no_document`：已按规则尝试，但本轮没有形成可用文档。", "- `manual_by_policy`：配置明确禁止自动访问，只允许人工处理。", "- `manual_collected`：人工策略来源已有合规导入内容。", "- `pending_entity_review`：原文已保全，但尚未完成主体消歧和内容解析，不能作为已核实事件。", "- `manual_import_pending_review`：文件由人工合法下载、授权导出或用户提供后导入；需核对 URL、主体与日期。", "- `manual_import_verified_mirror`：文件、哈希和元数据已核验，但实际字节来自公开镜像，仍保持镜像证据级。", "- `manual_import_verified_official`：文件来自 S0/S1 且与官方哈希精确一致。", "- `manual_import_rejected`：人工导入审核未通过，不计入有效主体文档。", "- `manual_review`：访问控制、robots、验证码或授权条件需要人工判断；自动任务不会绕过。", "- `cooldown`：服务端限频，所有出口共享冷却，不通过切换代理规避。", ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
