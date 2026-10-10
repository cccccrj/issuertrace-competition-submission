#!/usr/bin/env python3
"""Re-extract mutable metadata without changing preserved content versions."""
from __future__ import annotations

import argparse
from pathlib import Path

from collector.collect import ROOT, audit, connect, extract_metadata, load_sources, relevant_links


def main(source_ids: set[str] | None = None) -> None:
    selected = source_ids or set()
    _, sources = load_sources(
        ROOT / "config" / "sources.yaml", selected, None, include_manual=True)
    source_map = {source.source_id: source for source in sources}
    db = connect()
    if selected:
        placeholders = ",".join("?" for _ in selected)
        rows = db.execute(f"""SELECT version_id,source_id,final_url,raw_path,title
            FROM document_versions WHERE source_id IN ({placeholders})""",
                          tuple(sorted(selected))).fetchall()
    else:
        rows = db.execute(
            "SELECT version_id,source_id,final_url,raw_path,title FROM document_versions").fetchall()
    updated = 0
    missing = 0
    for version_id, source_id, final_url, raw_path, title in rows:
        source = source_map.get(source_id)
        path = ROOT / raw_path
        if not source or not path.exists():
            missing += 1
            continue
        body = path.read_bytes()
        fallback_title = relevant_links(final_url, body, source)[0] if path.suffix == ".html" else (title or "")
        metadata = extract_metadata(source, final_url, body, fallback_title)
        hint = db.execute("""SELECT title,published_at,company_id,matched_legal_entity,document_type,original_timezone,review_status
            FROM discovered_metadata WHERE source_id=? AND canonical_url=(SELECT canonical_url FROM document_versions WHERE version_id=?)""",
            (source_id, version_id)).fetchone()
        hint_review = hint[6] if hint else None
        if hint:
            for key, value in zip(("title", "published_at", "company_id", "matched_legal_entity", "document_type", "original_timezone"), hint[:6]):
                if (key == "published_at" and metadata.get("published_at") and
                        metadata.get("published_basis") in {
                            "article_dateline", "document_signature", "structured_data", "labeled_page_field",
                        }):
                    continue
                if value is not None:
                    metadata[key] = value
        review = hint_review or metadata.get("review_suggestion") or "pending_entity_review"
        if metadata["published_at"]:
            day = str(metadata["published_at"])[:10].replace("/", "-")
            if len(day) == 10 and day < str(source.raw.get("backfill_start", "0000-00-00")):
                review = "outside_backfill_window"
        if hint and not hint_review and metadata.get("company_id") and review != "outside_backfill_window":
            review = "auto_matched_strong_identifier"
        db.execute("""UPDATE document_versions SET company_id=?,matched_legal_entity=?,title=?,author=?,
            published_at=?,original_timezone=?,language=?,document_type=?,review_status=?,
            page_updated_at=?,published_basis=? WHERE version_id=?""",
            (metadata["company_id"], metadata["matched_legal_entity"], metadata["title"], metadata["author"],
             metadata["published_at"], metadata["original_timezone"], metadata["language"],
             metadata["document_type"], review, metadata["page_updated_at"], metadata["published_basis"], version_id))
        db.execute("UPDATE documents SET title=?,review_status=? WHERE source_id=? AND canonical_url=(SELECT canonical_url FROM document_versions WHERE version_id=?)",
                   (metadata["title"], review, source_id, version_id))
        updated += 1
        if updated % 10 == 0:
            db.commit()
    db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "metadata_reindexed",
           "selected_sources": sorted(selected), "updated_versions": updated,
           "missing_raw_files": missing})
    print(f"updated_versions={updated} missing_raw_files={missing}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-extract metadata without changing raw evidence")
    parser.add_argument("--source", action="append", default=[],
                        help="source_id to reindex; repeatable (default: all)")
    args = parser.parse_args()
    main(set(args.source))
