#!/usr/bin/env python3
"""Verify the derived analytical layer against immutable collection evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from collector.collect import ROOT, connect
from processor import pipeline, propagation, timeline
from processor.pipeline import EVENT_TYPES, IMPACT_OBJECTS, ISSUE_RULES, PRODUCTION_MODES


def json_values(value: str, field: str, allowed: set[str], errors: list[str], record_id: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        errors.append(f"{record_id}: {field} is invalid JSON")
        return []
    if not isinstance(parsed, list):
        errors.append(f"{record_id}: {field} is not a list")
        return []
    unexpected = set(parsed) - allowed
    if unexpected:
        errors.append(f"{record_id}: {field} has unexpected values {sorted(unexpected)}")
    # Membership was the only thing checked here, so 396 rows carrying the same
    # code twice in one array passed: 200 in affected_objects, 138 in
    # event_types, 57 in issue_codes, 1 in event_subjects.  A duplicate is not a
    # harmless repeat -- it doubles that value's weight in every count drawn off
    # these columns, and three of the rows it produced hold five event types
    # under a schema whose maxItems is four.
    duplicates = sorted({value for value in parsed if parsed.count(value) > 1})
    if duplicates:
        errors.append(f"{record_id}: {field} has duplicate values {duplicates}")
    return parsed


def json_field(value: str, field: str, errors: list[str], record_id: str) -> list[dict[str, Any]] | None:
    """Parse a stored JSON list of objects, reporting rather than raising."""
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        errors.append(f"{record_id}: {field} is invalid JSON")
        return None
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        errors.append(f"{record_id}: {field} is not a list of objects")
        return None
    return parsed


def risk_errors(event: sqlite3.Row, errors: list[str], record_id: str) -> None:
    """Check that a risk score follows from its row and equals its breakdown.

    Neither property held before 2026-09-06.  ``risk_parts`` reads six columns
    and the narrow sidecars rewrite some of them long after extraction --
    ``production_mode``'s re-classification moves ``SECONDHAND_MODES`` membership,
    worth -10 -- so 296 events carried a score derived from a production mode
    they no longer have, 52 of them in the wrong band, and the bands feed the
    timeline and the alert gate.  A score nothing can re-derive is a number, not
    a measurement.
    """
    label = pipeline.risk_label_from_row([event[column] for column in pipeline.RISK_INPUT_COLUMNS])
    expected, expected_level, _ = pipeline.risk(label)
    stored = float(event["risk_score"])
    if abs(expected - stored) > 0.05:
        errors.append(f"{record_id}: risk_score {stored} is not what the current columns imply ({expected})")
    if event["risk_level"] != expected_level:
        errors.append(f"{record_id}: risk_level {event['risk_level']} disagrees with the score ({expected_level})")
    breakdown = json_field(event["risk_breakdown_json"], "risk_breakdown", errors, record_id)
    if breakdown is not None:
        total = round(sum(float(part.get("points", 0)) for part in breakdown), 1)
        if abs(total - stored) > 0.05:
            errors.append(f"{record_id}: risk_breakdown sums to {total}, not to risk_score {stored}")


def count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def coverage_metrics(db: sqlite3.Connection) -> dict[str, int]:
    """Count processing coverage.

    Coverage compares like for like: versions rejected after they were processed stay in
    processed_documents as historical records but are not part of the processing scope,
    so counting every processed row against the non-rejected source set overstates coverage.
    """
    total = db.execute("SELECT count(*) FROM document_versions WHERE review_status NOT LIKE '%rejected%'").fetchone()[0]
    processed = db.execute("SELECT count(*) FROM processed_documents").fetchone()[0]
    in_scope = db.execute("""SELECT count(*) FROM processed_documents p
        JOIN document_versions v ON v.version_id=p.version_id
        WHERE v.review_status NOT LIKE '%rejected%'""").fetchone()[0]
    retained_rejected = db.execute("""SELECT count(*) FROM processed_documents p
        JOIN document_versions v ON v.version_id=p.version_id
        WHERE v.review_status LIKE '%rejected%'""").fetchone()[0]
    return {"source_versions": total, "processed_documents": processed,
            "processed_in_scope_versions": in_scope,
            "processed_rejected_versions": retained_rejected}


def verdict_metrics(db: sqlite3.Connection, errors: list[str], warnings: list[str], *,
                    require_verdicts: bool = False) -> dict[str, Any]:
    """Check that every stored verdict can be traced to a citable filing page.

    A verdict is only worth showing if a reader can open the filing and see the
    passage it rests on, so anything other than ``UNVERIFIABLE`` must carry a
    version and a page, and that page must exist in that version.
    """
    if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='claim_verdicts'").fetchone():
        return {"verdicts": 0}
    counts = {row[0]: row[1] for row in db.execute("SELECT verdict,count(*) FROM claim_verdicts GROUP BY 1")}
    methods = {row[0]: row[1] for row in db.execute("SELECT method,count(*) FROM claim_verdicts GROUP BY 1")}
    total = sum(counts.values())
    metrics: dict[str, Any] = {"verdicts": total, "verdict_breakdown": counts, "verdict_methods": methods}

    orphan = db.execute("""SELECT count(*) FROM claim_verdicts v
        LEFT JOIN event_claims c ON c.claim_id=v.claim_id WHERE c.claim_id IS NULL""").fetchone()[0]
    metrics["orphan_verdicts"] = orphan
    if orphan:
        errors.append(f"verdicts without a claim: {orphan}")

    uncited = db.execute("""SELECT count(*) FROM claim_verdicts
        WHERE verdict<>'UNVERIFIABLE' AND (evidence_version_id IS NULL OR evidence_page IS NULL)""").fetchone()[0]
    if uncited:
        errors.append(f"{uncited} verdicts assert a finding without citing a filing page")

    off_page = db.execute("""SELECT count(*) FROM claim_verdicts v
        JOIN processed_documents p ON p.version_id=v.evidence_version_id
        WHERE v.evidence_page IS NOT NULL
          AND (v.evidence_page < 1 OR (p.page_count IS NOT NULL AND v.evidence_page > p.page_count))""").fetchone()[0]
    if off_page:
        errors.append(f"{off_page} verdicts cite a page outside the filing")

    missing_evidence = db.execute("""SELECT count(*) FROM claim_verdicts v
        LEFT JOIN document_versions d ON d.version_id=v.evidence_version_id
        WHERE v.evidence_version_id IS NOT NULL AND d.version_id IS NULL""").fetchone()[0]
    if missing_evidence:
        errors.append(f"{missing_evidence} verdicts cite a version that is not in the evidence chain")

    # A company whose authoritative filings were never collected is reported as
    # such, so "unverifiable" is never silently read as "we checked and failed".
    no_corpus = methods.get("no_corpus", 0)
    metrics["no_corpus_verdicts"] = no_corpus
    if no_corpus:
        warnings.append(f"{no_corpus} claims are unverifiable because their company has no authoritative corpus")

    pending = db.execute("""SELECT count(*) FROM event_claims c
        LEFT JOIN claim_verdicts v ON v.claim_id=c.claim_id WHERE v.claim_id IS NULL""").fetchone()[0]
    metrics["claims_without_verdict"] = pending
    if pending:
        (errors if require_verdicts else warnings).append(f"{pending} claims have no verdict yet")
    return metrics


def has_table(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def analysis_metrics(db: sqlite3.Connection, errors: list[str], warnings: list[str], *,
                     require_analysis: bool = False) -> dict[str, Any]:
    """Check the propagation, timeline and alert layer against its own promises.

    Each check exists because the corresponding claim would otherwise be
    unfalsifiable: an alert that reports a conflict must be able to show the
    contradicted claim and the page it was checked against, a timeline position
    must name the field it was taken from, and a phase-adjusted score must be
    reproducible from the score and the coefficient it was multiplied by.
    """
    metrics: dict[str, Any] = {}
    if has_table(db, "disclosure_phases"):
        phases = db.execute("""SELECT company_id,phase,ordinal,started_at,ended_at
            FROM disclosure_phases ORDER BY company_id,ordinal""").fetchall()
        metrics["disclosure_phases"] = len(phases)
        by_company: dict[str, list[sqlite3.Row]] = {}
        for row in phases:
            by_company.setdefault(row["company_id"], []).append(row)
        for company_id, rows in by_company.items():
            for earlier, later in zip(rows, rows[1:]):
                if earlier["started_at"] and later["started_at"] and earlier["started_at"] > later["started_at"]:
                    errors.append(f"{company_id}: disclosure phases are not in date order at {later['phase']}")
                if earlier["ended_at"] != later["started_at"]:
                    errors.append(f"{company_id}: phase {earlier['phase']} does not end where {later['phase']} starts")

    if has_table(db, "propagation_edges"):
        kinds = {row[0]: row[1] for row in db.execute("SELECT edge_kind,count(*) FROM propagation_edges GROUP BY 1")}
        metrics["propagation_edges"] = sum(kinds.values())
        metrics["propagation_edge_kinds"] = kinds
        unexpected = set(kinds) - set(propagation.EDGE_KINDS)
        if unexpected:
            errors.append(f"propagation edges with unknown kind: {sorted(unexpected)}")
        dangling = db.execute("""SELECT count(*) FROM propagation_edges e
            LEFT JOIN document_versions f ON f.version_id=e.from_version_id
            LEFT JOIN document_versions t ON t.version_id=e.to_version_id
            WHERE f.version_id IS NULL OR t.version_id IS NULL""").fetchone()[0]
        if dangling:
            errors.append(f"{dangling} propagation edges point at a version outside the evidence chain")
        loops = db.execute("SELECT count(*) FROM propagation_edges WHERE from_version_id=to_version_id").fetchone()[0]
        if loops:
            errors.append(f"{loops} propagation edges link a version to itself")
        # A copy that predates its own original is a direction error, not a scoop.
        backwards = db.execute(f"""SELECT count(*) FROM propagation_edges
            WHERE lag_hours < 0 AND edge_kind IN ({','.join('?' for _ in propagation.DERIVATION_KINDS)})""",
            propagation.DERIVATION_KINDS).fetchone()[0]
        metrics["backwards_copy_edges"] = backwards
        if backwards:
            errors.append(f"{backwards} copy edges run from a later document to an earlier one")

    if has_table(db, "propagation_component_members"):
        metrics["propagation_components"] = db.execute("SELECT count(*) FROM propagation_components").fetchone()[0]
        uncovered = db.execute("""SELECT count(*) FROM event_records e
            LEFT JOIN propagation_component_members m ON m.version_id=e.version_id
            WHERE m.version_id IS NULL""").fetchone()[0]
        metrics["events_without_component"] = uncovered
        if uncovered:
            errors.append(f"{uncovered} events belong to no propagation component")
        mismatched = db.execute("""SELECT count(*) FROM propagation_components c
            WHERE c.member_count <> (SELECT count(*) FROM propagation_component_members m
                                     WHERE m.component_id=c.component_id)""").fetchone()[0]
        if mismatched:
            errors.append(f"{mismatched} propagation components have a wrong member count")

    if has_table(db, "event_timeline"):
        rows = db.execute("""SELECT event_id,timeline_at,time_basis,phase_coefficient,
            base_risk_score,adjusted_risk_score,claim_count,new_fact_count FROM event_timeline""").fetchall()
        metrics["timeline_rows"] = len(rows)
        metrics["timeline_time_basis"] = dict(Counter(row["time_basis"] for row in rows))
        missing = db.execute("""SELECT count(*) FROM event_records e
            LEFT JOIN event_timeline t ON t.event_id=e.event_id WHERE t.event_id IS NULL""").fetchone()[0]
        metrics["events_without_timeline"] = missing
        if missing:
            (errors if require_analysis else warnings).append(f"{missing} events have no timeline row")
        # A timeline position later than the report it came from is a plan or a
        # deadline placed as if it had happened (I-04).
        if "published_at" in {row[1] for row in db.execute("PRAGMA table_info(document_versions)")}:
            ahead = db.execute("""SELECT count(*) FROM event_timeline t
                JOIN document_versions v ON v.version_id=t.version_id
                WHERE t.time_basis='event_time' AND v.published_at IS NOT NULL
                  AND date(substr(t.timeline_at,1,10)) > date(substr(v.published_at,1,10), '+1 day')""").fetchone()[0]
            metrics["timeline_rows_after_publication"] = ahead
            if ahead:
                errors.append(f"{ahead} timeline rows sit after their own publication date")
        for row in rows:
            if row["time_basis"] not in timeline.TIME_BASES:
                errors.append(f"{row['event_id']}: unexpected time_basis {row['time_basis']}")
            elif row["time_basis"] != "none" and not row["timeline_at"]:
                errors.append(f"{row['event_id']}: time_basis {row['time_basis']} without a time")
            elif row["time_basis"] == "none" and row["timeline_at"]:
                errors.append(f"{row['event_id']}: timeline time recorded with no basis")
            expected, _ = timeline.adjusted_risk(float(row["base_risk_score"]), float(row["phase_coefficient"]))
            if abs(expected - float(row["adjusted_risk_score"])) > 0.05:
                errors.append(f"{row['event_id']}: adjusted risk {row['adjusted_risk_score']} does not "
                              f"follow from {row['base_risk_score']}×{row['phase_coefficient']}")
            if int(row["new_fact_count"]) > int(row["claim_count"]):
                errors.append(f"{row['event_id']}: more new facts than claims")
        undated = sum(1 for row in rows if row["time_basis"] == "none")
        if undated:
            warnings.append(f"{undated} events sit on the timeline with no usable date")

    if has_table(db, "alerts"):
        alert_rows = db.execute("""SELECT alert_id,lead_event_id,reasons_json,evidence_json,
            event_ids_json,credibility,adjusted_risk_score FROM alerts""").fetchall()
        metrics["alerts"] = len(alert_rows)
        known_events = {row[0] for row in db.execute("SELECT event_id FROM event_records")}
        with_claims = {row[0] for row in db.execute("SELECT event_id FROM event_claims")}
        for row in alert_rows:
            codes = {item.get("code") for item in json.loads(row["reasons_json"] or "[]")}
            evidence = json.loads(row["evidence_json"] or "[]")
            if not codes:
                errors.append(f"{row['alert_id']}: alert without a stated reason")
            if row["lead_event_id"] not in known_events:
                errors.append(f"{row['alert_id']}: lead event is not in the event ledger")
            elif row["lead_event_id"] not in with_claims:
                errors.append(f"{row['alert_id']}: lead event has no atomic claim to cite")
            if unknown := set(json.loads(row["event_ids_json"] or "[]")) - known_events:
                errors.append(f"{row['alert_id']}: merged events not in the ledger: {len(unknown)}")
            if "claim_conflict" in codes and not any(
                    item.get("verdict") == "CONFLICT" and item.get("evidence_page") for item in evidence):
                errors.append(f"{row['alert_id']}: reports a conflict without a cited filing page")
            if not 0 <= float(row["credibility"]) <= 1:
                errors.append(f"{row['alert_id']}: credibility outside 0..1")

    if has_table(db, "review_tasks"):
        # Review tasks name a row in an analytical table by id, and those tables
        # are rebuilt whenever their inputs change.  A task pointing at a row
        # that no longer exists cannot be judged and still counts in the
        # evaluation denominator, so it has to be loud rather than latent.
        dangling = dict(db.execute("""SELECT kind, count(*) FROM review_tasks t WHERE
            (t.kind='diff' AND NOT EXISTS(SELECT 1 FROM disclosure_diffs d WHERE d.diff_id=t.target_id))
            OR (t.kind='verdict' AND NOT EXISTS(SELECT 1 FROM event_claims c WHERE c.claim_id=t.target_id))
            OR (t.kind='event' AND NOT EXISTS(SELECT 1 FROM event_records e WHERE e.event_id=t.target_id))
            GROUP BY kind""").fetchall())
        metrics["review_tasks"] = db.execute("SELECT count(*) FROM review_tasks").fetchone()[0]
        metrics["dangling_review_tasks"] = dangling
        for kind, count in sorted(dangling.items()):
            errors.append(f"{count} {kind} review tasks point at a row that no longer exists")

    if has_table(db, "review_pre_annotations"):
        # An annotation outliving its task keeps counting in the agreement
        # denominator, which silently changes the rate it reports.
        orphaned = db.execute("""SELECT count(*) FROM review_pre_annotations
            WHERE task_id NOT IN (SELECT task_id FROM review_tasks)""").fetchone()[0]
        metrics["pre_annotations"] = db.execute(
            "SELECT count(*) FROM review_pre_annotations").fetchone()[0]
        if orphaned:
            errors.append(f"{orphaned} pre-annotations belong to a task that no longer exists")
    return metrics


def verify(*, require_ai_complete: bool = False, require_verdicts: bool = False,
           require_analysis: bool = False) -> dict[str, Any]:
    db = connect()
    db.row_factory = sqlite3.Row
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {}

    coverage = coverage_metrics(db)
    total, processed = coverage["source_versions"], coverage["processed_documents"]
    metrics.update(coverage)
    if coverage["processed_in_scope_versions"] != total:
        errors.append(f"processed coverage mismatch: {coverage['processed_in_scope_versions']}/{total}")
    missing_processed = db.execute("""SELECT count(*) FROM document_versions v
        LEFT JOIN processed_documents p ON p.version_id=v.version_id
        WHERE v.review_status NOT LIKE '%rejected%' AND p.version_id IS NULL""").fetchone()[0]
    orphan_processed = db.execute("""SELECT count(*) FROM processed_documents p
        LEFT JOIN document_versions v ON v.version_id=p.version_id WHERE v.version_id IS NULL""").fetchone()[0]
    metrics.update({"missing_processed_versions": missing_processed,
                    "orphan_processed_versions": orphan_processed})
    if missing_processed or orphan_processed:
        errors.append(f"processed version set mismatch: missing={missing_processed} orphan={orphan_processed}")

    rows = db.execute("SELECT version_id,text_path,text_hash,text_chars,extraction_status,inclusion_status,run_id FROM processed_documents").fetchall()
    ai_eligible = ai_done = 0
    status_counts: Counter[str] = Counter()
    for row in rows:
        status_counts[row["extraction_status"]] += 1
        if row["extraction_status"] == "ok" and row["text_chars"] >= 80 and row["inclusion_status"] == "included":
            ai_eligible += 1
            if str(row["run_id"]).startswith("ai-"):
                ai_done += 1
        if row["extraction_status"] == "ok":
            if not row["text_path"]:
                errors.append(f"{row['version_id']}: ok extraction has no text path")
                continue
            path = ROOT / row["text_path"]
            if not path.is_file():
                errors.append(f"{row['version_id']}: missing parsed text {row['text_path']}")
                continue
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != row["text_hash"]:
                errors.append(f"{row['version_id']}: parsed text hash mismatch")
    metrics["extraction_status"] = dict(status_counts)
    metrics.update({"ai_eligible_documents": ai_eligible, "ai_processed_documents": ai_done})
    if require_ai_complete and ai_done != ai_eligible:
        errors.append(f"AI coverage incomplete: {ai_done}/{ai_eligible}")

    events = db.execute("SELECT * FROM event_records").fetchall()
    metrics["events"] = len(events)
    orphan_events = db.execute("""SELECT count(*) FROM event_records e
        LEFT JOIN processed_documents p ON p.version_id=e.version_id WHERE p.version_id IS NULL""").fetchone()[0]
    metrics["orphan_events"] = orphan_events
    if orphan_events:
        errors.append(f"events without processed document: {orphan_events}")
    allowed_types = set(EVENT_TYPES)
    allowed_issues = set(ISSUE_RULES)
    allowed_modes = set(PRODUCTION_MODES)
    allowed_objects = set(IMPACT_OBJECTS)
    for event in events:
        event_id = event["event_id"]
        if event["eventhood"] != "EVENT":
            errors.append(f"{event_id}: non-EVENT row stored in event_records")
        json_values(event["event_types_json"], "event_types", allowed_types, errors, event_id)
        json_values(event["issue_codes_json"], "issue_codes", allowed_issues, errors, event_id)
        json_values(event["affected_objects_json"], "affected_objects", allowed_objects, errors, event_id)
        if event["production_mode"] not in allowed_modes:
            errors.append(f"{event_id}: unexpected production_mode {event['production_mode']}")
        if event["impact_direction"] not in {"positive", "negative", "neutral", "uncertain"}:
            errors.append(f"{event_id}: unexpected impact_direction")
        if not 0 <= float(event["relevance_score"]) <= 1:
            errors.append(f"{event_id}: relevance_score outside 0..1")
        if not 0 <= float(event["extraction_confidence"]) <= 1:
            errors.append(f"{event_id}: confidence outside 0..1")
        risk_errors(event, errors, event_id)

    raw_claim_count = db.execute("SELECT count(*) FROM event_claims").fetchone()[0]
    claims = db.execute("""SELECT c.*,p.text_path FROM event_claims c
        JOIN event_records e ON e.event_id=c.event_id
        JOIN processed_documents p ON p.version_id=e.version_id""").fetchall()
    metrics["claims"] = raw_claim_count
    metrics["traceable_claims"] = len(claims)
    if raw_claim_count != len(claims):
        errors.append(f"orphan/untraceable claims: {raw_claim_count - len(claims)}")
    events_without_claims = db.execute("""SELECT count(*) FROM event_records e
        LEFT JOIN event_claims c ON c.event_id=e.event_id WHERE c.claim_id IS NULL""").fetchone()[0]
    metrics["events_without_claims"] = events_without_claims
    if events_without_claims:
        warnings.append(f"{events_without_claims} machine events have no traceable atomic claim and require review")
    text_cache: dict[str, str] = {}
    for claim in claims:
        path_value = claim["text_path"]
        if not path_value:
            errors.append(f"{claim['claim_id']}: claim has no parsed text")
            continue
        text = text_cache.setdefault(path_value, (ROOT / path_value).read_text(encoding="utf-8"))
        start, end = int(claim["source_start"]), int(claim["source_end"])
        if start < 0 or end <= start or end > len(text):
            errors.append(f"{claim['claim_id']}: invalid source range {start}:{end}/{len(text)}")
        elif text[start:end] != claim["text"]:
            errors.append(f"{claim['claim_id']}: source range does not reproduce claim text")

    metrics.update(verdict_metrics(db, errors, warnings, require_verdicts=require_verdicts))
    metrics.update(analysis_metrics(db, errors, warnings, require_analysis=require_analysis))

    clusters = db.execute("SELECT cluster_id,member_count FROM duplicate_clusters").fetchall()
    metrics["duplicate_clusters"] = len(clusters)
    for cluster in clusters:
        actual = db.execute("SELECT count(*) FROM duplicate_cluster_members WHERE cluster_id=?",
                            (cluster["cluster_id"],)).fetchone()[0]
        if actual != cluster["member_count"]:
            errors.append(f"{cluster['cluster_id']}: cluster count {actual}!={cluster['member_count']}")

    issue_count = db.execute("SELECT count(*) FROM issue_ledger").fetchone()[0]
    metrics["issues"] = issue_count
    export_specs = {
        "events.jsonl": len(events),
        "issues.jsonl": issue_count,
        "processed_documents.jsonl": processed,
        "claims.jsonl": raw_claim_count,
        "duplicate_members.jsonl": db.execute("SELECT count(*) FROM duplicate_cluster_members").fetchone()[0],
    }
    for filename, table in (("propagation_edges.jsonl", "propagation_edges"),
                            ("timeline.jsonl", "event_timeline"), ("alerts.jsonl", "alerts")):
        if has_table(db, table):
            export_specs[filename] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    for filename, expected in export_specs.items():
        path = ROOT / "data" / "processed" / "exports" / filename
        if not path.is_file():
            errors.append(f"missing export: {filename}")
        else:
            actual = count_lines(path)
            if actual != expected:
                errors.append(f"{filename}: export rows {actual}!={expected}")
    for filename in ("events.csv", "issues.csv", "processing_summary.md", "quality_metrics.json"):
        if not (ROOT / "data" / "processed" / "exports" / filename).is_file():
            errors.append(f"missing export: {filename}")

    metrics.update({"errors": len(errors), "warnings": len(warnings), "ok": not errors})
    result = {"metrics": metrics, "errors": errors[:100], "warnings": warnings[:100]}
    db.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify processed data, claims, clusters and exports")
    parser.add_argument("--require-ai-complete", action="store_true")
    parser.add_argument("--require-verdicts", action="store_true",
                        help="fail when any claim still lacks an adjudication")
    parser.add_argument("--require-analysis", action="store_true",
                        help="fail when any event is missing from the timeline")
    args = parser.parse_args()
    result = verify(require_ai_complete=args.require_ai_complete, require_verdicts=args.require_verdicts,
                    require_analysis=args.require_analysis)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["metrics"]["ok"] else 1)


if __name__ == "__main__":
    main()
