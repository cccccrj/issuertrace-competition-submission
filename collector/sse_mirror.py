#!/usr/bin/env python3
"""Obtain quarantined SSE documents from the publisher's other host.

``static.sse.com.cn`` began answering with an Aliyun ``acw_sc__v2`` challenge on
2026-08-24 and was quarantined host-wide, which is correct and stays correct.
``sse_ipo_review`` was then quarantined too, for one reason only: it shares that
file host.  Its own registered host, ``www.sse.com.cn``, was never tested.

It should have been.  The exchange serves the review-stage filings from both
hosts, and on ``www`` the same path returns the PDF: robots absent, no challenge
marker, 6.7MB of ``%PDF``.  Twenty-two prospectus drafts, inquiry-letter replies
and legal opinions sat in the manual queue because a quarantine was inherited
rather than measured.

This is not circumvention and must not become it.  Nothing here injects a
cookie, replays a challenge token, spoofs a fingerprint or changes egress: it
asks a second publicly documented host of the same publisher for the same file,
through the same robots check, redirect guard, size cap, challenge detector and
rate gate as any other request.  If ``www`` starts challenging as well, the
detector quarantines it and this route closes -- which is the intended outcome,
not a failure to work around.

The exchange URL stays the citable one: a row that is obtained here is marked
``superseded`` with the host that actually served it, the same bookkeeping used
when a filing arrives from CNINFO.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

from collector.document_types import canonical
from collector.collect import (
    ROOT, active_cooldown, audit, blocked_host, canonicalize, collect_source, connect,
    enqueue_frontier, finish_frontier, load_sources, now_iso, url_in_scope,
)

# Only ever between hosts the publisher itself serves the same path from, and
# only from a quarantined host to one that is not.  Not a proxy list.
HOST_FALLBACKS = {"static.sse.com.cn": "www.sse.com.cn"}


def fallback_url(url: str) -> str | None:
    """The same document on the publisher's other host, or None if there is none."""
    parsed = urlparse(url)
    replacement = HOST_FALLBACKS.get((parsed.hostname or "").lower())
    if not replacement:
        return None
    return canonicalize(urlunparse(parsed._replace(netloc=replacement)))


def stranded_rows(db, source_id: str) -> list[tuple[str, str]]:
    """Queued rows whose only problem is the quarantined file host.

    A row parked for any other reason -- an oversize body, a missing file -- is
    left alone: this route changes where a document is asked for, not whether the
    reason it failed still applies.
    """
    rows = []
    for url, reason in db.execute(
            """SELECT url, last_error FROM crawl_frontier
               WHERE source_id=? AND status='manual_review' ORDER BY url""", (source_id,)):
        host = (urlparse(url).hostname or "").lower()
        if host not in HOST_FALLBACKS:
            continue
        if not blocked_host(db, source_id, host):
            continue
        rows.append((url, reason or ""))
    return rows


def carry_metadata(db, source_id: str, original: str, target: str) -> bool:
    """Move the discovery hint onto the host the file will actually come from.

    ``record_document`` reads ``discovered_metadata`` keyed by the URL it fetched.
    The hint was written against the exchange URL, so a document pulled from the
    other host arrives with no company, no publication date and a document type
    guessed from the response -- ``pdf`` instead of ``prospectus``.  It is then
    collected, parsed, and invisible to the evidence index and the version
    comparison, which select by type.  Same failure as a filing typed
    "announcement": the file is there and nothing can cite it.
    """
    row = db.execute("""SELECT title,published_at,company_id,matched_legal_entity,document_type,
                               original_timezone,review_status
                          FROM discovered_metadata WHERE source_id=? AND canonical_url=?""",
                     (source_id, original)).fetchone()
    if not row:
        return False
    fields = list(row)
    fields[4] = canonical(fields[4])  # document_type, in the processors' vocabulary
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
         document_type,original_timezone,review_status)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=excluded.title,published_at=excluded.published_at,company_id=excluded.company_id,
        matched_legal_entity=excluded.matched_legal_entity,document_type=excluded.document_type,
        original_timezone=excluded.original_timezone,review_status=excluded.review_status""",
        (source_id, target, *fields))
    db.commit()
    return True


# These filings run to tens of megabytes and the conservative profile allows a
# 30 second read, so a timeout here is ordinary rather than a verdict on the
# host.  The collector's own backoff decides how long to wait; this only decides
# how long to keep waiting before giving the run back.
MAX_COOLDOWN_WAIT_SECONDS = 900
ATTEMPTS_PER_DOCUMENT = 3


def cooldown_wait(db, source_id: str) -> float | None:
    row = db.execute("SELECT state,cooldown_until FROM source_state WHERE source_id=?",
                     (source_id,)).fetchone()
    if not row or row[0] == "manual_review" or not row[1]:
        return None
    remaining = (datetime.fromisoformat(row[1]) - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return 0.0
    return remaining if remaining <= MAX_COOLDOWN_WAIT_SECONDS else None


def recover(source_id: str, contact: str, limit: int) -> dict[str, int]:
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {source_id}, None)
    if not sources:
        raise SystemExit(f"unknown or manual-only source: {source_id}")
    source = sources[0]
    db = connect()
    recovered = refused = skipped = 0
    attempts: dict[str, int] = {}
    try:
        queue = [row for row in stranded_rows(db, source_id)][:limit]
        pending = list(queue)
        while pending:
            original, _reason = pending.pop(0)
            target = fallback_url(original)
            if not target or not url_in_scope(source, target):
                skipped += 1
                continue
            host = urlparse(target).hostname
            if blocked_host(db, source_id, host):
                break
            waiting = cooldown_wait(db, source_id)
            if waiting:
                time.sleep(waiting + 1)
                pending.insert(0, (original, _reason))
                continue
            if waiting is None and active_cooldown(db, source_id):
                break
            carry_metadata(db, source_id, original, target)
            enqueue_frontier(db, source_id, target, original, 0)
            collect_source(db, source, f"ListedCompanyEventIntelligence/0.1 (+{contact})",
                           1, False, target)
            saved = db.execute("SELECT status,raw_path FROM documents WHERE source_id=? AND canonical_url=?",
                               (source_id, canonicalize(target))).fetchone()
            if saved and saved[0] == 200 and saved[1]:
                finish_frontier(db, source_id, target, "done")
                finish_frontier(db, source_id, original, "superseded",
                                f"obtained from {host}: {target}")
                audit({"source_id": source_id, "outcome": "sse_mirror_success",
                       "url": original, "acquisition_url": target})
                recovered += 1
            else:
                attempts[original] = attempts.get(original, 0) + 1
                if attempts[original] < ATTEMPTS_PER_DOCUMENT:
                    pending.append((original, _reason))
                    continue
                finish_frontier(db, source_id, target, "manual_review", "fallback host did not serve it")
                audit({"source_id": source_id, "outcome": "sse_mirror_refused",
                       "url": original, "acquisition_url": target,
                       "attempts": attempts[original]})
                refused += 1
    finally:
        db.close()
    return {"recovered": recovered, "refused": refused, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="source_id whose file host is quarantined")
    parser.add_argument("--limit", type=int, default=5, help="documents to attempt (default: 5)")
    args = parser.parse_args()
    contact = os.environ.get("COLLECTOR_CONTACT")
    if not contact:
        raise SystemExit("COLLECTOR_CONTACT is required")
    result = recover(args.source, contact, args.limit)
    print(json.dumps({"source_id": args.source, **result, "at": now_iso()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
