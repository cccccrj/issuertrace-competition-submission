#!/usr/bin/env python3
"""Official SSE IPO-review API adapter with daily, low-volume synchronization."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from collector.document_types import canonical
from collector.collect import (
    ROOT,
    audit,
    block_host,
    canonicalize,
    collect_source,
    connect,
    enqueue_frontier,
    finish_frontier,
    load_sources,
)

SOURCE_ID = "sse_ipo_review"
ENDPOINT = "https://query.sse.com.cn/commonSoaQuery.do"
STATIC_PREFIX = "https://static.sse.com.cn/stock"


def parse_jsonp(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("SSE IPO API response contains no JSON object")
    return json.loads(raw[start:end + 1])


def ensure_tables(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS sse_ipo_projects (
        audit_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, company_name TEXT NOT NULL,
        audit_apply_date TEXT, update_date TEXT, current_status TEXT, registration_result TEXT,
        raw_query_url TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS sse_ipo_sync (
        source_id TEXT PRIMARY KEY, last_synced_at TEXT, last_error TEXT)""")
    db.commit()


def compact_date(value: object) -> str | None:
    digits = str(value or "")[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def document_type(item: dict) -> str:
    mapping = str(item.get("fileTypeMap") or "")
    if mapping.startswith("I001") or item.get("fileType") == 30:
        return "prospectus"
    if mapping in {"I3010", "I3020"}:
        return "review_inquiry_or_response"
    if mapping.startswith("I20"):
        return "listing_committee_document"
    if mapping == "I1010":
        return "registration_result"
    if mapping == "I1020":
        return "termination_decision"
    return "listing_document"


def store_hint(db, config: dict, url: str, title: str | None, published_at: str | None,
               doc_type: str, review_status: str = "auto_matched_strong_identifier") -> None:
    # The review system's own file-type names are not the names the processors
    # select on; an unmapped one is collected and then never cited.
    doc_type = canonical(doc_type) or doc_type
    company_id = "cxmt"
    legal_name = config["entities"][company_id]["canonical_name"]
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,published_at=excluded.published_at,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,document_type=excluded.document_type,
        original_timezone=excluded.original_timezone,review_status=excluded.review_status""",
        (SOURCE_ID, canonicalize(url), title, published_at, company_id, legal_name,
         doc_type, "Asia/Shanghai", review_status))


def ingest_listing(db, config: dict, payload: dict, query_url: str) -> str:
    rows = ((payload.get("pageHelp") or {}).get("data") or payload.get("result") or [])
    legal_name = config["entities"]["cxmt"]["canonical_name"]
    exact = [row for row in rows if row.get("stockAuditName") == legal_name]
    if len(exact) != 1:
        raise ValueError(f"expected one exact CXMT IPO project, received {len(exact)}")
    row = exact[0]
    audit_id = str(row.get("stockAuditNum") or "")
    if not audit_id:
        raise ValueError("IPO project has no stockAuditNum")
    detail_url = f"https://www.sse.com.cn/listing/renewal/ipo/index_listing_detail.shtml?auditId={audit_id}"
    store_hint(db, config, detail_url, f"{legal_name}发行上市审核项目", compact_date(row.get("updateDate")),
               "ipo_review_project")
    db.execute("""INSERT INTO sse_ipo_projects
        (audit_id,company_id,company_name,audit_apply_date,update_date,current_status,
         registration_result,raw_query_url,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(audit_id) DO UPDATE SET company_name=excluded.company_name,
        audit_apply_date=excluded.audit_apply_date,update_date=excluded.update_date,
        current_status=excluded.current_status,registration_result=excluded.registration_result,
        raw_query_url=excluded.raw_query_url,updated_at=excluded.updated_at""",
        (audit_id, "cxmt", legal_name, compact_date(row.get("auditApplyDate")),
         compact_date(row.get("updateDate")), str(row.get("currStatus") or ""),
         str(row.get("registeResult") or ""), canonicalize(query_url),
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.commit()
    return audit_id


def ingest_files(db, config: dict, payload: dict, query_url: str, audit_id: str) -> int:
    items = payload.get("result") or []
    prior_block = db.execute("""SELECT reason FROM host_state
        WHERE source_id='sse_announcements' AND host='static.sse.com.cn'
        AND state='manual_review'""").fetchone()
    if prior_block:
        block_host(db, SOURCE_ID, "static.sse.com.cn",
                   f"shared host previously quarantined: {prior_block[0]}")
    discovered = 0
    for item in items:
        path = item.get("filePath")
        if not path:
            continue
        pdf_url = canonicalize(STATIC_PREFIX.rstrip("/") + "/" + str(path).lstrip("/"))
        store_hint(db, config, pdf_url, item.get("fileTitle"), compact_date(item.get("fileUpdTime")),
                   document_type(item))
        enqueue_frontier(db, SOURCE_ID, pdf_url, canonicalize(query_url), 0)
        if prior_block:
            finish_frontier(db, SOURCE_ID, pdf_url, "manual_review",
                            "static.sse.com.cn already quarantined; no probe attempted")
        discovered += 1
    db.commit()
    audit({"source_id": SOURCE_ID, "outcome": "ipo_files_discovered", "audit_id": audit_id,
           "items_discovered": discovered, "static_host_preblocked": bool(prior_block)})
    return discovered


def latest_saved_payload(db, sql_id: str) -> tuple[str, dict] | None:
    row = db.execute("""SELECT canonical_url,raw_path FROM documents
        WHERE source_id=? AND canonical_url LIKE ? ORDER BY fetched_at DESC LIMIT 1""",
        (SOURCE_ID, f"%sqlId={sql_id}%")).fetchone()
    if not row or not (ROOT / row[1]).exists():
        return None
    return row[0], parse_jsonp((ROOT / row[1]).read_text(encoding="utf-8"))


def listing_url(callback: str) -> str:
    params = {
        "sqlId": "SH_XM_LB", "keyword": "长鑫科技集团股份有限公司",
        "issueMarketType": "1,2", "currStatus": "", "province": "", "csrcCode": "",
        "auditApplyDateBegin": "2024-01-01 00:00:00", "auditApplyDateEnd": "",
        "order": "updateDate|desc,stockAuditNum|desc", "isPagination": "true",
        "pageHelp.cacheSize": 1, "pageHelp.beginPage": 1, "pageHelp.endPage": 1,
        "pageHelp.pageSize": 25, "pageHelp.pageNo": 1, "jsonCallBack": callback,
    }
    return ENDPOINT + "?" + urlencode(params)


def files_url(audit_id: str, callback: str) -> str:
    return ENDPOINT + "?" + urlencode({"sqlId": "GP_COMMON_FILE_SEARCH",
                                        "auditId": audit_id, "jsonCallBack": callback})


def process_if_due(contact: str) -> int:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    source = sources[0]
    db = connect()
    ensure_tables(db)
    sync = db.execute("SELECT last_synced_at FROM sse_ipo_sync WHERE source_id=?", (SOURCE_ID,)).fetchone()
    if sync and sync[0] and datetime.fromisoformat(sync[0]) + timedelta(days=1) > datetime.now(timezone.utc):
        db.close()
        return 0
    try:
        listing = latest_saved_payload(db, "SH_XM_LB")
        files = latest_saved_payload(db, "GP_COMMON_FILE_SEARCH")
        if not listing or not files or sync:
            stamp = hashlib.sha256(datetime.now(timezone.utc).date().isoformat().encode()).hexdigest()[:10]
            user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
            list_url = listing_url("jsonpIpo" + stamp)
            store_hint(db, config, list_url, "长鑫科技发行上市审核项目查询", None,
                       "api_response", "api_response")
            db.commit()
            # The announcements adapter may have just used the same query host.
            # Keep an explicit cross-adapter gap instead of treating source IDs as separate quotas.
            time.sleep(10)
            collect_source(db, source, user_agent, 1, False, list_url)
            listing = latest_saved_payload(db, "SH_XM_LB")
            if not listing:
                raise ValueError("IPO project API response was not saved")
            audit_id = ingest_listing(db, config, listing[1], listing[0])
            time.sleep(10)
            file_url = files_url(audit_id, "jsonpIpoFiles" + stamp)
            store_hint(db, config, file_url, "长鑫科技发行上市审核文件查询", None,
                       "api_response", "api_response")
            db.commit()
            collect_source(db, source, user_agent, 1, False, file_url)
            files = latest_saved_payload(db, "GP_COMMON_FILE_SEARCH")
            if not files:
                raise ValueError("IPO file API response was not saved")
        audit_id = ingest_listing(db, config, listing[1], listing[0])
        discovered = ingest_files(db, config, files[1], files[0], audit_id)
        synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("""INSERT INTO sse_ipo_sync(source_id,last_synced_at,last_error) VALUES(?,?,NULL)
            ON CONFLICT(source_id) DO UPDATE SET last_synced_at=excluded.last_synced_at,last_error=NULL""",
                   (SOURCE_ID, synced_at))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "ipo_api_sync_success",
               "audit_id": audit_id, "items_discovered": discovered})
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        db.execute("""INSERT INTO sse_ipo_sync(source_id,last_error) VALUES(?,?)
            ON CONFLICT(source_id) DO UPDATE SET last_error=excluded.last_error""",
                   (SOURCE_ID, str(exc)[:300]))
        db.commit()
        audit({"source_id": SOURCE_ID, "outcome": "ipo_api_sync_error", "error": str(exc)[:300]})
    finally:
        db.close()
    return 1


if __name__ == "__main__":
    import os
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    process_if_due(contact)
