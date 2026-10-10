#!/usr/bin/env python3
"""Repair two defects in the stored labels, deterministically and once.

Both are local: no model is called, nothing is re-extracted, and neither touches
``event_claims`` or ``claim_verdicts``.

**Duplicates inside the controlled-vocabulary arrays.**  ``ai_label``'s schema
never declared ``uniqueItems``, and a single-window document returns its window's
labels directly instead of through ``merge_labels.union``, which is where
de-duplication happened.  396 rows carry a value twice — 200 in
``affected_objects``, 138 in ``event_types``, 57 in ``issue_codes``, 1 in
``event_subjects`` — and three of them hold five event types under a schema whose
``maxItems`` is four.  A duplicate is not a harmless repeat: every count drawn off
these columns weights that value twice.  Order is preserved so the first value a
document reported stays first.

**Risk scores that no longer follow from their row.**  ``risk_parts`` reads six
columns, and the narrow sidecars rewrite some of them long after extraction —
``production_mode``'s re-classification moves ``SECONDHAND_MODES`` membership,
worth −10.  Writing that column without replaying the score left 296 events
scored on a production mode they no longer carry, 52 of them in the wrong band,
and the bands feed ``timeline.adjusted_risk`` and the alert gate.  ``store()`` now
ends in ``pipeline.refresh_risk``, so this pass is for the backlog rather than for
the next occurrence.

Run ``processor.finalize`` afterwards: the timeline and the alerts are derived
from ``risk_score``, and 52 band changes move the alert surface.
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from collector.collect import audit, connect
from processor.pipeline import (
    RISK_INPUT_COLUMNS, ensure_schema, refresh_risk, risk, risk_label_from_row,
)

ARRAY_COLUMNS = ("event_types_json", "issue_codes_json", "affected_objects_json",
                 "event_subjects_json")
# The four array columns' own ceilings, as declared in ``ai_label.schema()``.
# De-duplicating can leave a row under its limit that was over it only because a
# value appeared twice; a row still over the limit after de-duplication is
# reported rather than truncated, because dropping a value is a labelling
# decision and this module only removes repeats.
ARRAY_LIMITS = {"event_types_json": 4, "issue_codes_json": 6, "affected_objects_json": 5}


def deduplicate(db: sqlite3.Connection) -> dict[str, int]:
    """Remove repeated values inside each controlled-vocabulary array, in order."""
    changed = {column: 0 for column in ARRAY_COLUMNS}
    over_limit: dict[str, int] = {}
    columns = ",".join(ARRAY_COLUMNS)
    for row in db.execute(f"SELECT event_id,{columns} FROM event_records").fetchall():
        event_id = row[0]
        for offset, column in enumerate(ARRAY_COLUMNS, start=1):
            values = json.loads(row[offset] or "[]")
            unique = list(dict.fromkeys(values))
            if len(unique) == len(values):
                continue
            db.execute(f"UPDATE event_records SET {column}=? WHERE event_id=?",
                       (json.dumps(unique, ensure_ascii=False), event_id))
            changed[column] += 1
            if column in ARRAY_LIMITS and len(unique) > ARRAY_LIMITS[column]:
                over_limit[column] = over_limit.get(column, 0) + 1
    summary = {f"deduplicated_{column}": count for column, count in changed.items()}
    summary["still_over_max_items"] = sum(over_limit.values())
    return summary


def refresh_all_risk(db: sqlite3.Connection) -> dict[str, int]:
    """Replay every event's risk score from its current columns."""
    columns = ",".join(RISK_INPUT_COLUMNS)
    rows = db.execute(f"""SELECT event_id,risk_score,risk_level,risk_breakdown_json,{columns}
                          FROM event_records""").fetchall()
    scores = levels = breakdowns = 0
    for row in rows:
        event_id, stored_score, stored_level, stored_breakdown = row[0], row[1], row[2], row[3]
        expected, expected_level, _ = risk(risk_label_from_row(tuple(row[4:])))
        if abs(expected - float(stored_score)) > 0.05:
            scores += 1
        if expected_level != stored_level:
            levels += 1
        if not json.loads(stored_breakdown or "[]"):
            breakdowns += 1
        refresh_risk(db, event_id)
    return {"events": len(rows), "risk_score_corrected": scores,
            "risk_level_corrected": levels, "breakdown_first_written": breakdowns}


def repair(*, dedupe: bool = True, risk_scores: bool = True) -> dict[str, int]:
    db = connect()
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    summary: dict[str, int] = {}
    # De-duplication runs first so the breakdown is written against the arrays the
    # pages will show.  It cannot change any score: ``risk_parts`` reads the arrays
    # as sets, and a repeat was never a second hit.
    if dedupe:
        summary.update(deduplicate(db))
    if risk_scores:
        summary.update(refresh_all_risk(db))
    db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "processed_labels_repaired", **summary})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair duplicate label arrays and stale risk scores")
    parser.add_argument("--only", choices=("dedupe", "risk"),
                        help="run just one of the two repairs (default: both)")
    args = parser.parse_args()
    summary = repair(dedupe=args.only != "risk", risk_scores=args.only != "dedupe")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
