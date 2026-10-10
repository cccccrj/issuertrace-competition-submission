#!/usr/bin/env python3
"""Derive a company's disclosure phase calendar from its own filing chain.

The same sentence carries a different amount of risk depending on when it is
published.  A rumour during the week a book is being built moves the price
investors pay; the same rumour a year after listing is background noise.  So the
risk score of an event is corrected by the phase its publication date falls in.

The calendar is *derived*, never hand-entered.  Every phase boundary is anchored
to a filing that exists in the immutable evidence chain, with its version id and
title recorded, so a reader can check why a date was treated as a boundary.  A
hard-coded calendar would drift from the evidence the moment a filing is added.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from collector.collect import audit, connect
from processor.pipeline import configured_companies, utc_now

# Only filings can move the calendar.  A news article titled 「上市公告书解读」
# must never be able to declare the company listed.
ANCHOR_DOCUMENT_TYPES = ("prospectus", "review_responses", "listing_documents", "announcements")


@dataclass(frozen=True)
class Phase:
    code: str
    label: str
    anchors: tuple[str, ...]
    coefficient: float
    rationale: str


# Ordered by regulatory sequence.  A phase whose anchor filing was never
# collected is simply absent from the calendar rather than guessed at.
PHASES: tuple[Phase, ...] = (
    Phase("PRE_FILING", "申报前", (), 1.0,
          "尚未进入审核程序，市场影响有限"),
    Phase("FILING", "申报受理与问询", ("申报稿",), 1.1,
          "已进入交易所审核，披露口径开始受问询约束"),
    Phase("MEETING", "上会审议", ("上市委会议公告",), 1.25,
          "上市委审议窗口，未经核实的负面信息可能影响审核判断"),
    Phase("REGISTRATION", "提交注册", ("上市委会议结果公告", "注册稿"), 1.2,
          "已过会待注册，披露变更受证监会关注"),
    Phase("APPROVED", "注册生效待发行", ("注册批复",), 1.15,
          "注册已生效，进入发行准备"),
    Phase("ISSUANCE", "发行与询价", ("招股意向书",), 1.3,
          "询价与申购窗口，不实信息直接影响投资者定价决策"),
    Phase("LISTED", "上市后", ("上市公告书",), 1.0,
          "已进入二级市场常规信息披露"),
)
DEFAULT_COEFFICIENT = 1.0


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS disclosure_phases(
        phase_id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL, phase TEXT NOT NULL, phase_label TEXT NOT NULL,
        ordinal INTEGER NOT NULL, started_at TEXT, ended_at TEXT,
        coefficient REAL NOT NULL, rationale TEXT NOT NULL,
        anchor_version_id TEXT, anchor_title TEXT, updated_at TEXT NOT NULL,
        UNIQUE(company_id, phase))""")
    db.execute("CREATE INDEX IF NOT EXISTS disclosure_phases_company ON disclosure_phases(company_id,ordinal)")


def as_date(value: str | None) -> str | None:
    """Reduce a timestamp to its calendar date.

    Publication timestamps arrive as bare dates and as full ISO instants; phase
    boundaries are announced per day, so comparisons happen on the date alone.
    """
    text = (value or "").strip()
    return text[:10] if len(text) >= 10 else None


def anchor_rows(db: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in ANCHOR_DOCUMENT_TYPES)
    return db.execute(f"""SELECT version_id,title,published_at FROM document_versions
        WHERE company_id=? AND document_type IN ({placeholders})
          AND published_at IS NOT NULL AND review_status NOT LIKE '%rejected%'
        ORDER BY published_at""", (company_id, *ANCHOR_DOCUMENT_TYPES)).fetchall()


def calendar(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Turn a filing chain into dated phases.

    A phase starts on the earliest filing whose title contains one of its
    anchors, and ends when the next present phase starts.  Taking the earliest
    match matters: 「更新申报稿」 also contains 「申报稿」, and letting it win would
    move the start of the filing phase five months later than it happened.
    """
    found: list[dict[str, Any]] = []
    for ordinal, phase in enumerate(PHASES):
        matched = [row for row in rows
                   if any(anchor in str(row["title"] or "") for anchor in phase.anchors)]
        if phase.anchors and not matched:
            continue
        first = min(matched, key=lambda row: str(row["published_at"])) if matched else None
        found.append({"phase": phase.code, "phase_label": phase.label, "ordinal": ordinal,
                      "started_at": as_date(first["published_at"]) if first else None,
                      "ended_at": None, "coefficient": phase.coefficient,
                      "rationale": phase.rationale,
                      "anchor_version_id": first["version_id"] if first else None,
                      "anchor_title": first["title"] if first else None})
    # A later phase whose anchor predates an earlier one would make the calendar
    # non-monotonic; the filing chain is ordered by regulation, so that means a
    # mislabelled document rather than a genuine reordering.
    ordered: list[dict[str, Any]] = []
    for entry in found:
        if ordered and entry["started_at"] and str(ordered[-1]["started_at"] or "") > entry["started_at"]:
            continue
        ordered.append(entry)
    for earlier, later in zip(ordered, ordered[1:]):
        earlier["ended_at"] = later["started_at"]
    # The unanchored 申报前 phase only exists once something else anchors the
    # calendar; on its own it would claim a company has a phase we never dated.
    if len(ordered) == 1 and ordered[0]["phase"] == "PRE_FILING":
        return []
    return ordered


def phase_at(phases: list[dict[str, Any]], when: str | None) -> dict[str, Any]:
    """The phase a date falls in, defaulting to no correction at all.

    An event with no usable date, or a company with no filing chain, gets
    coefficient 1.0 and phase ``UNKNOWN`` rather than an assumed phase — the
    adjustment has to be defensible or absent.
    """
    date = as_date(when)
    unknown = {"phase": "UNKNOWN", "phase_label": "未知阶段", "coefficient": DEFAULT_COEFFICIENT,
               "rationale": "无可用日期或该主体没有可定位的披露阶段"}
    if not date or not phases:
        return unknown
    current = unknown
    for entry in phases:
        started = entry["started_at"]
        if started is None or started <= date:
            current = entry
        else:
            break
    return current


def load(db: sqlite3.Connection, company_id: str) -> list[dict[str, Any]]:
    rows = db.execute("""SELECT phase,phase_label,ordinal,started_at,ended_at,coefficient,rationale,
        anchor_version_id,anchor_title FROM disclosure_phases WHERE company_id=? ORDER BY ordinal""",
        (company_id,)).fetchall()
    return [dict(row) for row in rows]


def build(db: sqlite3.Connection, companies: set[str] | None = None) -> dict[str, Any]:
    ensure_schema(db)
    db.row_factory = sqlite3.Row
    targets = sorted(companies) if companies else [row[0] for row in db.execute(
        "SELECT DISTINCT company_id FROM document_versions WHERE company_id IS NOT NULL ORDER BY 1")]
    summary: dict[str, Any] = {"companies": {}, "phases": 0}
    for company_id in targets:
        phases = calendar(anchor_rows(db, company_id))
        db.execute("DELETE FROM disclosure_phases WHERE company_id=?", (company_id,))
        for entry in phases:
            phase_id = hashlib.sha256(f"phase\0{company_id}\0{entry['phase']}".encode()).hexdigest()[:24]
            db.execute("""INSERT INTO disclosure_phases(phase_id,company_id,phase,phase_label,ordinal,
                started_at,ended_at,coefficient,rationale,anchor_version_id,anchor_title,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (phase_id, company_id, entry["phase"], entry["phase_label"], entry["ordinal"],
                 entry["started_at"], entry["ended_at"], entry["coefficient"], entry["rationale"],
                 entry["anchor_version_id"], entry["anchor_title"], utc_now()))
        summary["companies"][company_id] = [
            {"phase": entry["phase"], "started_at": entry["started_at"], "ended_at": entry["ended_at"],
             "coefficient": entry["coefficient"], "anchor_title": entry["anchor_title"]}
            for entry in phases]
        summary["phases"] += len(phases)
    return summary


def run(companies: set[str] | None = None) -> dict[str, Any]:
    db = connect()
    db.row_factory = sqlite3.Row
    summary = build(db, companies)
    db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "disclosure_phases_built", "phases": summary["phases"]})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive disclosure phase calendars from the filing chain")
    parser.add_argument("--company", action="append", choices=configured_companies())
    args = parser.parse_args()
    print(json.dumps(run(set(args.company) if args.company else None), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
