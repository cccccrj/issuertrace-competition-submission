import json
import sqlite3
import unittest

from processor.verify import (
    analysis_metrics, coverage_metrics, json_values, risk_errors, verdict_metrics,
)


class CoverageMetricsTests(unittest.TestCase):
    def _db(self, versions):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, review_status TEXT)")
        db.execute("CREATE TABLE processed_documents(version_id TEXT PRIMARY KEY)")
        for version_id, review_status, processed in versions:
            db.execute("INSERT INTO document_versions VALUES(?,?)", (version_id, review_status))
            if processed:
                db.execute("INSERT INTO processed_documents VALUES(?)", (version_id,))
        return db

    def test_rejected_versions_processed_earlier_do_not_overstate_coverage(self):
        # A version rejected after processing stays on record but leaves the processing scope.
        db = self._db([("a", "candidate_entity_match", True),
                       ("b", "rejected_content_incomplete", True)])
        metrics = coverage_metrics(db)
        self.assertEqual(metrics["source_versions"], 1)
        self.assertEqual(metrics["processed_documents"], 2)
        self.assertEqual(metrics["processed_in_scope_versions"], 1)
        self.assertEqual(metrics["processed_rejected_versions"], 1)

    def test_unprocessed_in_scope_version_lowers_coverage(self):
        db = self._db([("a", "candidate_entity_match", True),
                       ("b", "candidate_entity_match", False)])
        metrics = coverage_metrics(db)
        self.assertEqual(metrics["source_versions"], 2)
        self.assertEqual(metrics["processed_in_scope_versions"], 1)
        self.assertEqual(metrics["processed_rejected_versions"], 0)


class VerdictMetricsTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE event_claims(claim_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE document_versions(version_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE processed_documents(version_id TEXT PRIMARY KEY, page_count INTEGER)")
        db.execute("""CREATE TABLE claim_verdicts(claim_id TEXT PRIMARY KEY, verdict TEXT,
                      evidence_version_id TEXT, evidence_page INTEGER, method TEXT)""")
        db.execute("INSERT INTO document_versions VALUES('v1')")
        db.execute("INSERT INTO processed_documents VALUES('v1', 372)")
        return db

    def _verdict(self, db, claim_id, verdict, version="v1", page=10, method="llm_adjudicated"):
        db.execute("INSERT INTO event_claims VALUES(?)", (claim_id,))
        db.execute("INSERT INTO claim_verdicts VALUES(?,?,?,?,?)", (claim_id, verdict, version, page, method))

    def test_missing_table_is_reported_as_zero_not_an_error(self):
        db = sqlite3.connect(":memory:")
        errors, warnings = [], []
        self.assertEqual(verdict_metrics(db, errors, warnings), {"verdicts": 0})
        self.assertEqual(errors, [])

    def test_a_cited_verdict_passes(self):
        db = self._db()
        errors, warnings = [], []
        self._verdict(db, "c1", "SUPPORTED")
        verdict_metrics(db, errors, warnings)
        self.assertEqual(errors, [])

    def test_a_finding_without_a_page_is_an_error(self):
        db = self._db()
        errors, warnings = [], []
        self._verdict(db, "c1", "CONFLICT", page=None)
        verdict_metrics(db, errors, warnings)
        self.assertTrue(any("without citing a filing page" in error for error in errors))

    def test_unverifiable_without_a_page_is_allowed(self):
        db = self._db()
        errors, warnings = [], []
        self._verdict(db, "c1", "UNVERIFIABLE", version=None, page=None, method="no_corpus")
        verdict_metrics(db, errors, warnings)
        self.assertEqual(errors, [])

    def test_a_page_beyond_the_filing_is_an_error(self):
        db = self._db()
        errors, warnings = [], []
        self._verdict(db, "c1", "SUPPORTED", page=999)
        verdict_metrics(db, errors, warnings)
        self.assertTrue(any("outside the filing" in error for error in errors))

    def test_a_verdict_citing_an_unknown_version_is_an_error(self):
        db = self._db()
        errors, warnings = [], []
        self._verdict(db, "c1", "SUPPORTED", version="ghost")
        verdict_metrics(db, errors, warnings)
        self.assertTrue(any("not in the evidence chain" in error for error in errors))

    def test_pending_claims_warn_by_default_and_fail_when_required(self):
        db = self._db()
        db.execute("INSERT INTO event_claims VALUES('c2')")
        errors, warnings = [], []
        metrics = verdict_metrics(db, errors, warnings)
        self.assertEqual(metrics["claims_without_verdict"], 1)
        self.assertEqual(errors, [])
        self.assertTrue(warnings)
        strict_errors: list[str] = []
        verdict_metrics(db, strict_errors, [], require_verdicts=True)
        self.assertTrue(strict_errors)

    def test_no_corpus_verdicts_are_surfaced_separately(self):
        db = self._db()
        errors, warnings = [], []
        self._verdict(db, "c1", "UNVERIFIABLE", version=None, page=None, method="no_corpus")
        metrics = verdict_metrics(db, errors, warnings)
        self.assertEqual(metrics["no_corpus_verdicts"], 1)
        self.assertTrue(any("no authoritative corpus" in warning for warning in warnings))


class AnalysisMetricsTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE document_versions(version_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE event_records(event_id TEXT PRIMARY KEY, version_id TEXT)")
        db.execute("CREATE TABLE event_claims(claim_id TEXT PRIMARY KEY, event_id TEXT)")
        db.execute("""CREATE TABLE event_timeline(event_id TEXT PRIMARY KEY, timeline_at TEXT,
            time_basis TEXT, phase_coefficient REAL, base_risk_score REAL, adjusted_risk_score REAL,
            claim_count INTEGER, new_fact_count INTEGER)""")
        db.execute("""CREATE TABLE propagation_edges(edge_id TEXT PRIMARY KEY, edge_kind TEXT,
            from_version_id TEXT, to_version_id TEXT, lag_hours REAL)""")
        for version_id in ("v1", "v2"):
            db.execute("INSERT INTO document_versions VALUES(?)", (version_id,))
        db.execute("INSERT INTO event_records VALUES('e1','v1')")
        db.execute("INSERT INTO event_claims VALUES('c1','e1')")
        db.execute("INSERT INTO event_timeline VALUES('e1','2026-07-10','published_at',1.3,55.0,71.5,1,1)")
        return db

    def _alerts(self, db, reasons_json, evidence_json, lead="e1"):
        db.execute("""CREATE TABLE IF NOT EXISTS alerts(alert_id TEXT PRIMARY KEY, lead_event_id TEXT,
            reasons_json TEXT, evidence_json TEXT, event_ids_json TEXT, credibility REAL,
            adjusted_risk_score REAL)""")
        db.execute("INSERT INTO alerts VALUES('a1',?,?,?,'[\"e1\"]',0.6,71.5)",
                   (lead, reasons_json, evidence_json))

    def test_a_consistent_analysis_layer_reports_no_errors(self):
        db = self._db()
        errors, warnings = [], []
        metrics = analysis_metrics(db, errors, warnings)
        self.assertEqual(errors, [])
        self.assertEqual(metrics["timeline_time_basis"], {"published_at": 1})

    def test_an_adjusted_score_that_does_not_follow_from_its_coefficient_is_an_error(self):
        db = self._db()
        db.execute("UPDATE event_timeline SET adjusted_risk_score=99.0")
        errors: list[str] = []
        analysis_metrics(db, errors, [])
        self.assertTrue(any("does not follow" in error for error in errors))

    def test_a_timeline_position_without_a_stated_basis_is_an_error(self):
        db = self._db()
        db.execute("UPDATE event_timeline SET time_basis='none'")
        errors: list[str] = []
        analysis_metrics(db, errors, [])
        self.assertTrue(any("no basis" in error for error in errors))

    def test_a_deadline_placed_as_if_it_had_happened_is_an_error(self):
        # I-04: a 2032 contract deadline reported in 2024 sat on the timeline at 2032.
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, published_at TEXT)")
        db.execute("CREATE TABLE event_records(event_id TEXT PRIMARY KEY, version_id TEXT)")
        db.execute("CREATE TABLE event_claims(claim_id TEXT PRIMARY KEY, event_id TEXT)")
        db.execute("""CREATE TABLE event_timeline(event_id TEXT PRIMARY KEY, version_id TEXT, timeline_at TEXT,
            time_basis TEXT, phase_coefficient REAL, base_risk_score REAL, adjusted_risk_score REAL,
            claim_count INTEGER, new_fact_count INTEGER)""")
        db.execute("INSERT INTO document_versions VALUES('v1','2024-05-10T08:00:00Z')")
        db.execute("INSERT INTO event_records VALUES('e1','v1')")
        db.execute("INSERT INTO event_timeline VALUES('e1','v1','2032-01-01','event_time',1.0,50.0,50.0,1,1)")
        errors: list[str] = []
        metrics = analysis_metrics(db, errors, [])
        self.assertEqual(metrics["timeline_rows_after_publication"], 1)
        self.assertTrue(any("after their own publication" in error for error in errors))
        db.execute("UPDATE event_timeline SET timeline_at='2024-05-10', time_basis='published_at'")
        errors = []
        analysis_metrics(db, errors, [])
        self.assertFalse(any("after their own publication" in error for error in errors))

    def test_an_event_missing_from_the_timeline_warns_and_can_be_made_fatal(self):
        db = self._db()
        db.execute("INSERT INTO event_records VALUES('e2','v2')")
        errors, warnings = [], []
        analysis_metrics(db, errors, warnings)
        self.assertEqual(errors, [])
        self.assertTrue(warnings)
        strict: list[str] = []
        analysis_metrics(db, strict, [], require_analysis=True)
        self.assertTrue(strict)

    def test_a_propagation_edge_pointing_outside_the_evidence_chain_is_an_error(self):
        db = self._db()
        db.execute("INSERT INTO propagation_edges VALUES('x','REPOST','v1','ghost',1.0)")
        errors: list[str] = []
        analysis_metrics(db, errors, [])
        self.assertTrue(any("outside the evidence chain" in error for error in errors))

    def test_a_copy_edge_running_backwards_in_time_is_an_error(self):
        # A reprint published before its own original is a direction bug.
        db = self._db()
        db.execute("INSERT INTO propagation_edges VALUES('x','REPOST','v1','v2',-48.0)")
        errors: list[str] = []
        analysis_metrics(db, errors, [])
        self.assertTrue(any("earlier one" in error for error in errors))

    def test_an_edge_linking_a_version_to_itself_is_an_error(self):
        db = self._db()
        db.execute("INSERT INTO propagation_edges VALUES('x','REPOST','v1','v1',0.0)")
        errors: list[str] = []
        analysis_metrics(db, errors, [])
        self.assertTrue(any("to itself" in error for error in errors))

    def test_an_alert_reporting_a_conflict_must_cite_a_page(self):
        db = self._db()
        self._alerts(db, '[{"code":"claim_conflict"}]', '[{"verdict":"CONFLICT","evidence_page":null}]')
        errors: list[str] = []
        analysis_metrics(db, errors, [])
        self.assertTrue(any("without a cited filing page" in error for error in errors))

    def test_an_alert_citing_its_conflict_passes(self):
        db = self._db()
        self._alerts(db, '[{"code":"claim_conflict"}]', '[{"verdict":"CONFLICT","evidence_page":24}]')
        errors: list[str] = []
        analysis_metrics(db, errors, [])
        self.assertEqual(errors, [])

    def test_an_alert_led_by_an_event_with_no_claim_is_an_error(self):
        db = self._db()
        db.execute("INSERT INTO event_records VALUES('e2','v2')")
        db.execute("INSERT INTO event_timeline VALUES('e2','2026-07-10','published_at',1.0,55.0,55.0,0,0)")
        self._alerts(db, '[{"code":"high_adjusted_risk"}]', "[]", lead="e2")
        errors: list[str] = []
        analysis_metrics(db, errors, [])
        self.assertTrue(any("no atomic claim" in error for error in errors))

    def test_a_missing_analysis_layer_is_reported_as_absent_not_broken(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE event_records(event_id TEXT PRIMARY KEY, version_id TEXT)")
        errors, warnings = [], []
        self.assertEqual(analysis_metrics(db, errors, warnings), {})
        self.assertEqual(errors, [])


class ProcessingVerifyTests(unittest.TestCase):
    def test_json_values_rejects_unknown_enum(self):
        errors = []
        values = json_values('["KNOWN", "OTHER"]', "labels", {"KNOWN"}, errors, "id")
        self.assertEqual(values, ["KNOWN", "OTHER"])
        self.assertEqual(len(errors), 1)

    def test_json_values_rejects_non_list(self):
        errors = []
        self.assertEqual(json_values('{"a":1}', "labels", {"KNOWN"}, errors, "id"), [])
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()


class ControlledArrayTests(unittest.TestCase):
    """Membership was checked; repetition was not.

    396 stored rows carry a value twice inside one array -- 200 in
    affected_objects, 138 in event_types, 57 in issue_codes -- and the verifier
    passed all of them.  A duplicate doubles that value's weight in every count
    drawn off the column, and three of these rows ended up holding five event
    types under a schema whose maxItems is four.
    """

    def test_a_repeated_value_is_an_error(self):
        errors: list[str] = []
        json_values('["FINANCIAL_AUDIT","FINANCIAL_AUDIT"]', "event_types",
                    {"FINANCIAL_AUDIT"}, errors, "e1")
        self.assertEqual(len(errors), 1)
        self.assertIn("duplicate", errors[0])

    def test_distinct_values_stay_clean(self):
        errors: list[str] = []
        json_values('["A","B"]', "event_types", {"A", "B"}, errors, "e1")
        self.assertEqual(errors, [])


class RiskInvariantTests(unittest.TestCase):
    """A risk score has to follow from the row and equal its own breakdown."""

    def _event(self, **overrides):
        from processor.pipeline import risk, risk_parts
        label = {"event_types": [], "issue_codes": [], "eventhood": "EVENT",
                 "impact_direction": "negative", "impact_magnitude": "major",
                 "production_mode": "ORIGINAL_INVESTIGATION"}
        score, level, _ = risk(label)
        row = {"eventhood": "EVENT", "event_types_json": "[]", "issue_codes_json": "[]",
               "production_mode": "ORIGINAL_INVESTIGATION", "impact_direction": "negative",
               "impact_magnitude": "major", "risk_score": score, "risk_level": level,
               "risk_breakdown_json": json.dumps([part.as_dict() for part in risk_parts(label)])}
        row.update(overrides)
        return row

    def test_a_consistent_event_reports_nothing(self):
        errors: list[str] = []
        risk_errors(self._event(), errors, "e1")
        self.assertEqual(errors, [])

    def test_a_score_left_behind_by_a_sidecar_is_an_error(self):
        # What production_mode's pass did to 296 events: rewrite the column and
        # leave the score describing the mode it replaced.
        errors: list[str] = []
        risk_errors(self._event(production_mode="BRIEF"), errors, "e1")
        self.assertTrue(any("current columns imply" in error for error in errors), errors)

    def test_a_breakdown_that_does_not_add_up_is_an_error(self):
        errors: list[str] = []
        risk_errors(self._event(risk_breakdown_json='[{"key":"base","points":15.0}]'), errors, "e1")
        self.assertTrue(any("sums to" in error for error in errors), errors)

    def test_a_band_disagreeing_with_the_score_is_an_error(self):
        errors: list[str] = []
        risk_errors(self._event(risk_level="low"), errors, "e1")
        self.assertTrue(any("disagrees with the score" in error for error in errors), errors)
