#!/usr/bin/env python3
"""Create missing immutable version rows from already-preserved raw documents."""
from __future__ import annotations

import hashlib

from collector.collect import ROOT, audit, connect, extract_metadata, load_sources


def main() -> None:
    _, sources = load_sources(ROOT / "config" / "sources.yaml", set(), None)
    source_map = {source.source_id: source for source in sources}
    db = connect()
    rows = db.execute("""SELECT d.source_id,d.canonical_url,d.final_url,d.fetched_at,d.content_type,
        d.content_hash,d.raw_path,d.title,d.network_route,d.review_status
        FROM documents d LEFT JOIN document_versions v
          ON v.source_id=d.source_id AND v.canonical_url=d.canonical_url
        WHERE v.version_id IS NULL""").fetchall()
    repaired = 0
    missing_raw = 0
    missing_source = 0
    for sid, canonical, final_url, fetched_at, content_type, digest, raw_path, title, route, stored_review in rows:
        source = source_map.get(sid)
        path = ROOT / raw_path
        if not source:
            missing_source += 1
            continue
        if not path.exists():
            missing_raw += 1
            continue
        metadata = extract_metadata(source, final_url or canonical, path.read_bytes(), title or "")
        hint = db.execute("""SELECT title,published_at,company_id,matched_legal_entity,
            document_type,original_timezone,review_status FROM discovered_metadata
            WHERE source_id=? AND canonical_url=?""", (sid, canonical)).fetchone()
        hint_review = hint[6] if hint else None
        if hint:
            for key, value in zip(("title", "published_at", "company_id", "matched_legal_entity",
                                   "document_type", "original_timezone"), hint[:6]):
                if value is not None:
                    metadata[key] = value
        review = hint_review or metadata.get("review_suggestion") or stored_review or "pending_entity_review"
        if metadata.get("published_at"):
            published_day = str(metadata["published_at"])[:10].replace("/", "-")
            if len(published_day) == 10 and published_day < str(source.raw.get("backfill_start", "0000-00-00")):
                review = "outside_backfill_window"
        if hint and not hint_review and metadata.get("company_id") and review != "outside_backfill_window":
            review = "auto_matched_strong_identifier"
        version_id = hashlib.sha256(f"{sid}\0{canonical}\0{digest}".encode()).hexdigest()
        is_pdf = "pdf" in str(content_type or "").lower() or path.suffix.lower() == ".pdf"
        db.execute("""INSERT OR IGNORE INTO document_versions
            (version_id,source_id,source_tier,company_id,matched_legal_entity,title,author,
             published_at,fetched_at,original_timezone,canonical_url,final_url,language,
             document_type,content_hash,attachment_hash,parent_version_id,paywall_status,
             collection_mode,network_route,raw_path,review_status,page_updated_at,published_basis)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (version_id, sid, source.raw["source_tier"], metadata.get("company_id"),
             metadata.get("matched_legal_entity"), metadata.get("title"), metadata.get("author"),
             metadata.get("published_at"), fetched_at, metadata.get("original_timezone"), canonical,
             final_url, metadata.get("language"), metadata.get("document_type"), digest,
             digest if is_pdf else None, None, str(source.raw.get("paywall", False)).lower(),
             source.raw.get("collection_mode"), route, raw_path, review,
             metadata.get("page_updated_at"), metadata.get("published_basis")))
        db.execute("UPDATE documents SET title=?,review_status=? WHERE source_id=? AND canonical_url=?",
                   (metadata.get("title"), review, sid, canonical))
        repaired += 1
    db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "missing_versions_repaired",
           "repaired": repaired, "missing_raw_files": missing_raw,
           "missing_source_configs": missing_source})
    print(f"repaired={repaired} missing_raw_files={missing_raw} missing_source_configs={missing_source}")


if __name__ == "__main__":
    main()
