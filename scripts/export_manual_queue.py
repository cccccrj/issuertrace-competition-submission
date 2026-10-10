#!/usr/bin/env python3
"""Export the URLs a source stopped on, as a worksheet for lawful manual retrieval.

A host that answers with a challenge page is quarantined rather than retried, so
its queue stops growing and nothing further is fetched.  That is the correct
behaviour, but it leaves the discovered URLs stranded inside the database where
nobody can act on them.  This writes them out — with the title, publication date
and the reason the crawler stopped — so a person can decide what to obtain
through legitimate means and import with ``collector.manual_import``.

Read-only: it never touches the frontier or marks anything as fetched.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys

from collector.collect import ROOT

DB_PATH = ROOT / "data" / "state" / "collector.sqlite3"

QUERY = """SELECT f.source_id, f.url, f.last_error, f.discovered_at,
                  d.title, d.published_at, d.company_id, d.document_type
    FROM crawl_frontier f
    LEFT JOIN discovered_metadata d ON d.source_id=f.source_id AND d.canonical_url=f.url
    WHERE f.status='manual_review'"""


def rows(db: sqlite3.Connection, source: str | None, company: str | None) -> list[sqlite3.Row]:
    query, parameters = QUERY, []
    if source:
        query += " AND f.source_id=?"
        parameters.append(source)
    if company:
        query += " AND d.company_id=?"
        parameters.append(company)
    # Newest first: a manual queue is worked from the current filings backwards.
    return db.execute(query + " ORDER BY d.published_at DESC, f.url", parameters).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="limit to one source_id, e.g. sse_announcements")
    parser.add_argument("--company", help="limit to one company_id")
    parser.add_argument("--output", help="write TSV here instead of stdout")
    arguments = parser.parse_args()

    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    found = rows(db, arguments.source, arguments.company)
    db.close()

    stream = open(arguments.output, "w", encoding="utf-8", newline="") if arguments.output else sys.stdout
    writer = csv.writer(stream, delimiter="\t")
    writer.writerow(["published_at", "company_id", "document_type", "title", "url",
                     "stopped_because", "discovered_at"])
    for row in found:
        writer.writerow([row["published_at"] or "", row["company_id"] or "", row["document_type"] or "",
                         (row["title"] or "").replace("\t", " "), row["url"],
                         row["last_error"] or "", row["discovered_at"] or ""])
    if arguments.output:
        stream.close()
        print(f"{len(found)} rows -> {arguments.output}")


if __name__ == "__main__":
    main()
