#!/usr/bin/env python3
"""Rebuild discovery queues from preserved HTML without making network requests."""
from __future__ import annotations

import argparse

from collector.collect import ROOT, attachment_metadata_hints, audit, connect, discovery_depth, enqueue_frontier, link_title_hints, listing_page_dates, load_sources, relevant_links, store_discovered_hint


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", default=[])
    args = parser.parse_args()
    _, sources = load_sources(ROOT / "config" / "sources.yaml", set(args.source), None)
    db = connect()
    added_before = db.total_changes
    scanned = 0
    for source in sources:
        rows = db.execute("""SELECT d.canonical_url,d.raw_path,COALESCE(f.depth,0)
            FROM documents d LEFT JOIN crawl_frontier f ON f.source_id=d.source_id AND f.url=d.canonical_url
            WHERE d.source_id=? AND lower(COALESCE(d.content_type,'')) LIKE '%html%'""", (source.source_id,)).fetchall()
        for url, raw_path, depth in rows:
            path = ROOT / raw_path
            if not path.exists():
                continue
            body = path.read_bytes()
            visible_dates = listing_page_dates(url, body)
            if visible_dates and max(visible_dates) < str(source.raw.get("backfill_start", "0000-00-00")):
                scanned += 1
                continue
            _, links = relevant_links(url, body, source)
            hints = link_title_hints(url, body, source)
            attachment_hints = attachment_metadata_hints(url, body, source)
            for link in links:
                child_depth = discovery_depth(int(depth), link, url)
                if child_depth > 3:
                    continue
                published = None
                if link.lower().endswith(".pdf"):
                    title, published = attachment_hints.get(link, (hints.get(link), None))
                    store_discovered_hint(db, source, link, title, published)
                enqueue_frontier(db, source.source_id, link, url, child_depth)
                if link.lower().endswith(".pdf") and published and published < str(source.raw.get("backfill_start", "0000-00-00")):
                    db.execute("UPDATE crawl_frontier SET status='skipped_scope',processed_at=CURRENT_TIMESTAMP,last_error='attachment predates backfill window' WHERE source_id=? AND url=?",
                               (source.source_id, link))
                    db.commit()
            scanned += 1
    changes = db.total_changes - added_before
    db.close()
    audit({"source_id": "system", "outcome": "frontier_rebuilt", "html_documents_scanned": scanned,
           "database_changes": changes, "selected_sources": args.source or "all"})
    print(f"html_documents_scanned={scanned} database_changes={changes}")


if __name__ == "__main__":
    main()
