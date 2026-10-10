#!/usr/bin/env python3
"""Order events on a defensible clock, mark what is new, and score by phase.

Three things the event ledger cannot answer on its own live here.

**Which time an event sits at.** Roughly one event in ten has no machine-readable
event time, so a timeline built only on ``event_time`` silently drops them and a
timeline built only on ``published_at`` dates a 2023 lawsuit to the day someone
wrote about it.  Every row therefore carries the basis its time came from, and
the basis is shown rather than assumed.

**What is actually new.**  Two different questions are answered separately:
``is_new_this_round`` — the event was not in the previous build — and
``new_fact_count`` — the event asserts something earlier events on the same issue
did not.  Conflating them turns a wave of reprints into a wave of news.

**What the date is worth.**  A claim published while a book is being built is
worth more attention than the same claim a year after listing, so the risk score
is multiplied by the coefficient of the disclosure phase the event falls in.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import timedelta
import hashlib
import json
import sqlite3
from typing import Any

from collector.collect import audit, connect
from processor import disclosure_phase
from processor.pipeline import add_column, event_time_bounds, tokenize, utc_now
from processor.propagation import as_moment, similarity

TIME_BASES = ("event_time", "published_at", "fetched_at", "none")
# Above this, a claim restates something already said on the issue.  Below it,
# the event brought a fact the issue did not have.
FACT_SIMILARITY = 0.60
# Bounded so one 250-event issue cannot make novelty scoring quadratic.
FACT_LOOKBACK = 200
RECENT_WINDOW_DAYS = 30
DORMANT_WINDOW_DAYS = 90
ISSUE_STATUSES = ("emerging", "escalating", "active", "cooling", "dormant", "empty")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS timeline_rounds(
        round_id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL, started_at TEXT NOT NULL,
        event_count INTEGER NOT NULL, new_event_count INTEGER NOT NULL,
        is_baseline INTEGER NOT NULL, reference_time TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS event_timeline(
        event_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, version_id TEXT NOT NULL,
        timeline_at TEXT, time_basis TEXT NOT NULL, time_precision TEXT,
        phase TEXT NOT NULL, phase_label TEXT NOT NULL, phase_coefficient REAL NOT NULL,
        base_risk_score REAL NOT NULL, adjusted_risk_score REAL NOT NULL,
        adjusted_risk_level TEXT NOT NULL, first_seen_round TEXT NOT NULL,
        is_new_this_round INTEGER NOT NULL, claim_count INTEGER NOT NULL,
        new_fact_count INTEGER NOT NULL, novelty REAL NOT NULL, updated_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS event_timeline_order ON event_timeline(company_id,timeline_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS issue_status_history(
        history_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, company_id TEXT NOT NULL,
        issue_code TEXT NOT NULL, status TEXT NOT NULL, previous_status TEXT,
        recent_event_count INTEGER NOT NULL, previous_event_count INTEGER NOT NULL,
        reason TEXT NOT NULL, round_id TEXT NOT NULL, changed_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS issue_status_history_issue
        ON issue_status_history(issue_id,changed_at)""")
    add_column(db, "issue_ledger", "status_reason", "TEXT")
    add_column(db, "issue_ledger", "status_changed_at", "TEXT")
    add_column(db, "issue_ledger", "recent_event_count", "INTEGER NOT NULL DEFAULT 0")
    add_column(db, "issue_ledger", "new_fact_count", "INTEGER NOT NULL DEFAULT 0")


def timeline_time(event: dict[str, Any]) -> tuple[str | None, str, str | None]:
    """Choose when an event goes on the timeline, and say where that came from.

    Normalized event time wins because it is when the thing happened.  Where it
    is missing, publication time is used and labelled as such — a reader can
    then see that the position on the axis is when it was reported, not when it
    occurred, instead of being quietly misled.

    A *scheduled* event time — a deadline or target still in the future when
    the report was written — is not when anything happened.  Placed at 2032 it
    took the phase coefficient of a phase that does not exist yet and dropped
    out of every activity count (I-04); what happened is that it was announced,
    so it goes on the axis at publication.
    """
    bounds = event_time_bounds(event.get("event_time"))
    if bounds and event.get("event_time_role") != "scheduled":
        return bounds[0], "event_time", event.get("event_time_precision") or "day"
    published = str(event.get("published_at") or "").strip()
    if published:
        return published, "published_at", "day" if len(published) <= 10 else "second"
    fetched = str(event.get("fetched_at") or "").strip()
    if fetched:
        return fetched, "fetched_at", "second"
    return None, "none", None


def adjusted_risk(base: float, coefficient: float) -> tuple[float, str]:
    score = round(min(100.0, max(0.0, base * coefficient)), 1)
    return score, "high" if score >= 75 else "medium" if score >= 45 else "low"


def novelty(claims: list[set[str]], seen: list[set[str]]) -> int:
    """Count how many of an event's claims say something the issue had not heard.

    Similarity is measured against earlier claims on the same issue, so a
    rewritten version of yesterday's sentence counts as nothing new even though
    no two characters line up.
    """
    new = 0
    for tokens in claims:
        if not tokens:
            continue
        window = seen[-FACT_LOOKBACK:]
        if all(similarity(tokens, earlier) < FACT_SIMILARITY for earlier in window):
            new += 1
    return new


def issue_status(times: list[str], reference: str) -> tuple[str, int, int, str]:
    """Describe where an issue stands, from its own event dates alone.

    "Now" is the latest event in the corpus rather than the wall clock: this is
    an archive, and judging a finished collection against today's date would
    report every issue as dormant.
    """
    moment = as_moment(reference)
    parsed = sorted(item for item in (as_moment(value) for value in times) if item)
    # An event time may legitimately point at a planned future date; such an
    # event is on the timeline but says nothing about how active the issue is now.
    parsed = [item for item in parsed if moment and item <= moment]
    if not moment or not parsed:
        return "empty", 0, 0, "没有可定位时间的事件"
    recent = [item for item in parsed if item > moment - timedelta(days=RECENT_WINDOW_DAYS)]
    previous = [item for item in parsed
                if moment - timedelta(days=2 * RECENT_WINDOW_DAYS) < item <= moment - timedelta(days=RECENT_WINDOW_DAYS)]
    counts = (len(recent), len(previous))
    if parsed[-1] <= moment - timedelta(days=DORMANT_WINDOW_DAYS):
        return "dormant", *counts, f"最近一次事件在 {DORMANT_WINDOW_DAYS} 天窗口之前"
    if not recent:
        return "cooling", *counts, f"最近 {RECENT_WINDOW_DAYS} 天没有新事件"
    if parsed[0] > moment - timedelta(days=RECENT_WINDOW_DAYS):
        return "emerging", *counts, f"专题首次出现在最近 {RECENT_WINDOW_DAYS} 天内"
    if len(recent) >= 2 and len(recent) > len(previous):
        return "escalating", *counts, f"最近 {RECENT_WINDOW_DAYS} 天 {len(recent)} 起，上一窗口 {len(previous)} 起"
    return "active", *counts, f"最近 {RECENT_WINDOW_DAYS} 天 {len(recent)} 起，持续跟进中"


def load_events(db: sqlite3.Connection, phases: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    claims: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for event_id, ordinal, text in db.execute("SELECT event_id,ordinal,text FROM event_claims"):
        claims[event_id].append((ordinal, text))
    events = []
    for row in db.execute("""SELECT e.event_id,e.version_id,e.company_id,e.event_time,e.event_time_precision,
                                    e.event_time_role,e.risk_score,e.issue_codes_json,v.published_at,v.fetched_at
        FROM event_records e JOIN document_versions v ON v.version_id=e.version_id"""):
        event = dict(row)
        event["issue_codes"] = json.loads(event.pop("issue_codes_json") or "[]")
        event["claim_tokens"] = [tokenize(text) for _, text in sorted(claims.get(event["event_id"], []))]
        at, basis, precision = timeline_time(event)
        phase = disclosure_phase.phase_at(phases.get(event["company_id"], []), at)
        score, level = adjusted_risk(float(event["risk_score"]), float(phase["coefficient"]))
        event.update({"timeline_at": at, "time_basis": basis, "time_precision": precision,
                      "phase": phase["phase"], "phase_label": phase["phase_label"],
                      "phase_coefficient": float(phase["coefficient"]),
                      "adjusted_risk_score": score, "adjusted_risk_level": level})
        events.append(event)
    return events


def open_round(db: sqlite3.Connection, reference: str | None) -> tuple[str, int, bool]:
    """Start a build round; the first one is a baseline, not a wave of news.

    Without this the first run would mark 1,600 archived events as new and fire
    an alert for each, which is exactly the noise the round exists to prevent.
    """
    previous = db.execute("SELECT count(*) FROM timeline_rounds").fetchone()[0]
    round_id = hashlib.sha256(f"round\0{previous}\0{utc_now()}".encode()).hexdigest()[:20]
    db.execute("""INSERT INTO timeline_rounds(round_id,ordinal,started_at,event_count,
        new_event_count,is_baseline,reference_time) VALUES(?,?,?,?,?,?,?)""",
        (round_id, previous, utc_now(), 0, 0, 1 if previous == 0 else 0, reference))
    return round_id, previous, previous == 0


def build(db: sqlite3.Connection) -> dict[str, Any]:
    db.row_factory = sqlite3.Row
    disclosure_phase.ensure_schema(db)
    ensure_schema(db)
    phases = {company_id: disclosure_phase.load(db, company_id) for (company_id,) in db.execute(
        "SELECT DISTINCT company_id FROM disclosure_phases")}
    events = load_events(db, phases)
    # "Now" is the newest thing the collector saw, not the newest date any event
    # mentions: an event about planned 2032 capacity would otherwise move the
    # clock six years forward and report every live issue as dormant.
    reference = max((str(event["published_at"] or event["fetched_at"] or "")
                     for event in events), default="") or None
    round_id, ordinal, is_baseline = open_round(db, reference)
    known = dict(db.execute("SELECT event_id,first_seen_round FROM event_timeline"))

    ordered = sorted(events, key=lambda item: (str(item["timeline_at"] or "9999"), item["event_id"]))
    seen_by_issue: dict[tuple[str, str], list[set[str]]] = defaultdict(list)
    new_this_round = 0
    now = utc_now()
    rows = []
    for event in ordered:
        issues = event["issue_codes"] or [None]
        keys = [(event["company_id"], issue) for issue in issues if issue]
        pool: list[set[str]] = []
        for key in keys:
            pool.extend(seen_by_issue[key])
        new_facts = novelty(event["claim_tokens"], pool)
        event["new_fact_count"] = new_facts
        for key in keys:
            seen_by_issue[key].extend(event["claim_tokens"])
        first_seen = known.get(event["event_id"], round_id)
        is_new = int(first_seen == round_id and not is_baseline)
        new_this_round += is_new
        claim_count = len(event["claim_tokens"])
        rows.append((event["event_id"], event["company_id"], event["version_id"], event["timeline_at"],
                     event["time_basis"], event["time_precision"], event["phase"], event["phase_label"],
                     event["phase_coefficient"], float(event["risk_score"]), event["adjusted_risk_score"],
                     event["adjusted_risk_level"], first_seen, is_new, claim_count, new_facts,
                     round(new_facts / claim_count, 3) if claim_count else 0.0, now))
    db.execute("DELETE FROM event_timeline")
    db.executemany("""INSERT INTO event_timeline(event_id,company_id,version_id,timeline_at,time_basis,
        time_precision,phase,phase_label,phase_coefficient,base_risk_score,adjusted_risk_score,
        adjusted_risk_level,first_seen_round,is_new_this_round,claim_count,new_fact_count,novelty,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    db.execute("UPDATE timeline_rounds SET event_count=?,new_event_count=? WHERE round_id=?",
               (len(rows), new_this_round, round_id))

    statuses = update_issue_status(db, events, reference, round_id)
    bases: dict[str, int] = defaultdict(int)
    for event in events:
        bases[event["time_basis"]] += 1
    return {"round_id": round_id, "round_ordinal": ordinal, "baseline": is_baseline,
            "reference_time": reference, "events": len(rows), "new_this_round": new_this_round,
            "time_basis": dict(sorted(bases.items())),
            "new_facts": sum(row[15] for row in rows),
            "phase_adjusted": sum(1 for event in events if event["phase_coefficient"] != 1.0),
            "issue_status": statuses}


def update_issue_status(db: sqlite3.Connection, events: list[dict[str, Any]], reference: str | None,
                        round_id: str) -> dict[str, int]:
    """Recompute issue status and record every transition.

    ``issue_ledger`` is rebuilt from scratch on every pipeline run, so status
    history has to live in its own table; otherwise "this issue escalated" would
    be a fact the system forgets each time it recomputes.
    """
    by_issue: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        for issue in event["issue_codes"]:
            by_issue[(event["company_id"], issue)].append(event)
    latest = {(row[0], row[1]): row[2] for row in db.execute("""SELECT company_id,issue_code,status
        FROM issue_status_history WHERE (issue_id,changed_at) IN
        (SELECT issue_id,max(changed_at) FROM issue_status_history GROUP BY issue_id)""")}
    counts: dict[str, int] = defaultdict(int)
    now = utc_now()
    for (company_id, issue), members in by_issue.items():
        status, recent, previous_count, reason = issue_status(
            [str(item["timeline_at"]) for item in members if item["timeline_at"]], reference or "")
        new_facts = sum(int(member.get("new_fact_count", 0)) for member in members)
        counts[status] += 1
        issue_id = hashlib.sha256(f"issue\0{company_id}\0{issue}".encode()).hexdigest()
        was = latest.get((company_id, issue))
        if was != status:
            history_id = hashlib.sha256(f"status\0{issue_id}\0{round_id}\0{status}".encode()).hexdigest()[:24]
            db.execute("""INSERT OR REPLACE INTO issue_status_history(history_id,issue_id,company_id,
                issue_code,status,previous_status,recent_event_count,previous_event_count,reason,
                round_id,changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (history_id, issue_id, company_id, issue, status, was, recent, previous_count,
                 reason, round_id, now))
        db.execute("""UPDATE issue_ledger SET status=?,status_reason=?,recent_event_count=?,
            new_fact_count=?,status_changed_at=COALESCE(?,status_changed_at) WHERE company_id=? AND issue_code=?""",
            (status, reason, recent, new_facts, now if was != status else None, company_id, issue))
    return dict(sorted(counts.items()))


def run() -> dict[str, Any]:
    db = connect()
    db.row_factory = sqlite3.Row
    summary = build(db)
    db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "timeline_built", "events": summary["events"],
           "new_this_round": summary["new_this_round"], "round_id": summary["round_id"]})
    return summary


def main() -> None:
    argparse.ArgumentParser(description="Build the event timeline, novelty and issue status").parse_args()
    print(json.dumps(run(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
