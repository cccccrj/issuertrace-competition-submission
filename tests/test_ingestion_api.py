"""The ingestion funnel — what was dropped, and why.

命题主要任务第 1 项's visible half is the event ledger. This is the other half:
4,339 versions were collected and 1,995 events came out, and until now nothing
said what happened in between. A pipeline that cannot account for what it
discarded is one whose output has to be taken on trust.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from tests.test_api import ApiTestCase


class IngestionTestCase(ApiTestCase):
    def seed(self):
        db = sqlite3.connect(self.fixture_path)
        # The shared fixture already has this table with three columns, and
        # CREATE TABLE IF NOT EXISTS will not widen it. Adding what this suite
        # needs, one column at a time, leaves the fixture's own definition alone.
        for column, definition in (
                ("text_chars", "INTEGER DEFAULT 0"),
                ("extraction_status", "TEXT NOT NULL DEFAULT 'ok'"),
                ("inclusion_status", "TEXT NOT NULL DEFAULT 'included'"),
                ("relevance_score", "REAL DEFAULT 0"),
                ("eventhood", "TEXT NOT NULL DEFAULT 'IRRELEVANT'"),
                ("rejection_reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("rule_eventhood", "TEXT"),
                ("rule_relevance_score", "REAL"),
                ("processed_at", "TEXT")):
            try:
                db.execute(f"ALTER TABLE processed_documents ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass   # already present
        for version, source in (("v-a", "cninfo"), ("v-b", "cninfo"), ("v-c", "media_x")):
            db.execute("""INSERT OR REPLACE INTO document_versions
                (version_id, source_id, company_id, title, published_at, source_tier)
                VALUES(?,?,?,?,?,?)""",
                (version, source, "cxmt", f"文档 {version}", "2026-08-01", "S0"))
        rows = [
            # Kept, and became an event.
            ("v-a", "ok", "included", "EVENT", "[]"),
            # Kept, but nothing eventful in it.
            ("v-b", "ok", "included", "NO_EVENT", '["event_without_traceable_claim"]'),
            # Dropped at the gate, and the same code fired twice on the one row —
            # which is why documents and occurrences are counted separately.
            ("v-c", "body_missing", "excluded_no_body", "IRRELEVANT",
             '["excluded_no_body", "unsupported_claim_dropped", "unsupported_claim_dropped"]'),
        ]
        db.executemany("""INSERT OR REPLACE INTO processed_documents
            (version_id, extraction_status, inclusion_status, eventhood, rejection_reasons_json)
            VALUES(?,?,?,?,?)""", rows)
        # The rule layer's own reading, which the model then wrote over. v-a is
        # an agreement, v-b is the rule layer overruled, v-c never reached the
        # model at all — it was excluded at the gate.
        for version, rule in (("v-a", "EVENT"), ("v-b", "EVENT"), ("v-c", "EVENT")):
            db.execute("UPDATE processed_documents SET rule_eventhood=? WHERE version_id=?",
                       (rule, version))
        db.commit()
        db.close()


class FunnelTests(IngestionTestCase):
    def test_the_stages_narrow_from_collected_to_verifiable(self):
        self.seed()
        stages = self.client.get("/api/ingestion/funnel").json()["stages"]
        counts = [stage["count"] for stage in stages]
        self.assertEqual([stage["key"] for stage in stages],
                         ["collected", "included", "eventful", "with_claims"])
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_a_repeated_reason_is_counted_both_ways(self):
        # One document carrying the same code twice is one document and two
        # occurrences. Reporting only one of those misstates the same fact, in
        # opposite directions depending which you pick.
        self.seed()
        reasons = {row["code"]: row for row in
                   self.client.get("/api/ingestion/funnel").json()["rejection_reasons"]}
        self.assertEqual(reasons["unsupported_claim_dropped"]["documents"], 1)
        self.assertEqual(reasons["unsupported_claim_dropped"]["occurrences"], 2)

    def test_reasons_are_ordered_by_how_many_documents_they_touched(self):
        self.seed()
        counts = [row["documents"] for row in
                  self.client.get("/api/ingestion/funnel").json()["rejection_reasons"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_every_reason_code_has_a_readable_label(self):
        self.seed()
        for row in self.client.get("/api/ingestion/funnel").json()["rejection_reasons"]:
            self.assertTrue(row["label"])

    def test_the_three_status_breakdowns_are_all_reported(self):
        self.seed()
        body = self.client.get("/api/ingestion/funnel").json()
        self.assertEqual({row["value"] for row in body["inclusion"]},
                         {"included", "excluded_no_body"})
        self.assertEqual({row["value"] for row in body["eventhood"]},
                         {"EVENT", "NO_EVENT", "IRRELEVANT"})
        self.assertEqual({row["value"] for row in body["extraction"]}, {"ok", "body_missing"})


class SourceTests(IngestionTestCase):
    def test_each_source_reports_how_much_survived(self):
        self.seed()
        items = {row["source_id"]: row for row in
                 self.client.get("/api/ingestion/sources").json()["items"]}
        self.assertEqual(items["cninfo"]["documents"], 2)
        self.assertEqual(items["cninfo"]["included"], 2)
        self.assertEqual(items["cninfo"]["events"], 1)

    def test_the_event_rate_is_what_makes_a_dead_source_noticeable(self):
        # A source whose documents never become events is either the wrong source
        # or a parser that has stopped working, and both should be visible before
        # somebody goes looking.
        self.seed()
        items = {row["source_id"]: row for row in
                 self.client.get("/api/ingestion/sources").json()["items"]}
        self.assertEqual(items["media_x"]["event_rate"], 0.0)
        self.assertEqual(items["cninfo"]["event_rate"], 0.5)

    def test_sources_are_ordered_by_volume(self):
        self.seed()
        counts = [row["documents"] for row in
                  self.client.get("/api/ingestion/sources").json()["items"]]
        self.assertEqual(counts, sorted(counts, reverse=True))


class DocumentTests(IngestionTestCase):
    def test_a_dropped_document_carries_the_reasons_it_was_dropped(self):
        self.seed()
        items = self.client.get("/api/ingestion/documents",
                                params={"inclusion_status": "excluded_no_body"}).json()["items"]
        self.assertEqual(len(items), 1)
        self.assertIn("excluded_no_body", items[0]["rejection_reasons"])

    def test_filters_narrow_independently(self):
        self.seed()
        self.assertEqual(
            self.client.get("/api/ingestion/documents", params={"eventhood": "EVENT"}).json()["total"], 1)
        self.assertEqual(
            self.client.get("/api/ingestion/documents", params={"source": "cninfo"}).json()["total"], 2)


class DegradationTests(ApiTestCase):
    def test_it_reports_unavailable_rather_than_failing_before_the_pipeline_runs(self):
        body = self.client.get("/api/ingestion/funnel").json()
        self.assertFalse(body["available"])
        self.assertEqual(body["stages"], [])


if __name__ == "__main__":
    unittest.main()


class LayerComparisonTests(IngestionTestCase):
    """Where the offline rule layer and the model disagree about event-hood.

    Both readings have been stored side by side since the eligibility floor was
    split off, and nothing had ever read them together. On the real corpus they
    disagree on 43% of the documents that reached the model.
    """

    def setUp(self):
        super().setUp()
        self.seed()
        # Two more, so all four cells are occupied and — the reason this matters
        # — `compared` (4) differs from `rule_events` (2). With the shared
        # fixture alone the two denominators were equal, and a rate computed
        # against the wrong one gave the same answer.
        db = sqlite3.connect(self.fixture_path)
        for version, rule, model in (("v-d", "NO_EVENT", "EVENT"), ("v-e", "NO_EVENT", "NO_EVENT")):
            db.execute("""INSERT OR REPLACE INTO document_versions
                (version_id, source_id, company_id, title, published_at, source_tier)
                VALUES(?,?,?,?,?,?)""", (version, "media_x", "cxmt", f"文档 {version}", "2026-08-01", "S2"))
            db.execute("""INSERT OR REPLACE INTO processed_documents
                (version_id, extraction_status, inclusion_status, eventhood,
                 rejection_reasons_json, rule_eventhood)
                VALUES(?,'ok','included',?,'[]',?)""", (version, model, rule))
        db.commit()
        db.close()

    def test_the_four_cases_partition_what_reached_the_model(self):
        body = self.client.get("/api/ingestion/layers").json()
        counts = {case["key"]: case["count"] for case in body["cases"]}
        self.assertEqual(sum(counts.values()), body["compared"])
        self.assertEqual(counts, {"agree_event": 1, "rule_only": 1,
                                  "system_only": 1, "agree_none": 1})

    def test_a_document_stopped_at_the_gate_is_outside_the_comparison(self):
        # v-c was never sent, so there is no second opinion to compare against.
        # Counting it would put a rule reading against a default.
        body = self.client.get("/api/ingestion/layers").json()
        self.assertEqual(body["compared"], 4)

    def test_the_overrule_rate_has_its_own_denominator(self):
        # Two different facts: how often the layers differ at all, and how often
        # the model specifically overturns a rule-layer event. Four documents
        # compared, two of them rule-layer events — so a rate taken over the
        # wrong denominator is 0.25 rather than 0.5 and cannot hide.
        body = self.client.get("/api/ingestion/layers").json()
        self.assertEqual(body["compared"], 4)
        self.assertEqual(body["rule_events"], 2)
        self.assertEqual(body["overruled"], 1)
        self.assertEqual(body["overrule_rate"], 0.5)
        self.assertEqual(body["disagreement_rate"], 0.5)

    def test_nothing_here_is_called_an_error_rate(self):
        # No human has adjudicated any of these, and the payload has to carry
        # that rather than leave the reader to assume the model is the one wrong.
        body = self.client.get("/api/ingestion/layers").json()
        self.assertEqual(body["judged"], 0)
        self.assertNotIn("accuracy", body)

    def test_each_cell_can_be_opened_and_returns_exactly_its_own_count(self):
        # The number on the matrix and the number in the list have to be the
        # same number. Without the gate carried into the filter they were not:
        # the filter answered 1,432 where the matrix said 757, the difference
        # being excluded documents that never reached the model.
        body = self.client.get("/api/ingestion/layers").json()
        for case in body["cases"]:
            listed = self.client.get("/api/ingestion/documents",
                                     params={"layer": case["key"], "limit": 1}).json()
            self.assertEqual(listed["total"], case["count"], case["key"])

    def test_the_listing_carries_both_readings(self):
        rows = self.client.get("/api/ingestion/documents",
                               params={"layer": "rule_only"}).json()["items"]
        self.assertEqual(rows[0]["rule_eventhood"], "EVENT")
        self.assertNotEqual(rows[0]["eventhood"], "EVENT")

    def test_an_unknown_case_is_refused_rather_than_ignored(self):
        # Silently dropping the filter would show the whole corpus under a
        # heading that says otherwise.
        self.assertEqual(self.client.get("/api/ingestion/documents",
                                         params={"layer": "nonsense"}).status_code, 400)

    def test_a_table_without_the_column_at_all_is_also_unavailable(self):
        # The column is checked as well as its contents. A database predating
        # the split has no column, and `PRAGMA table_info` is the only thing
        # that can tell the difference between「没有这一列」and「这一列是空的」.
        from api.routes.ingestion import layers_available
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE processed_documents(version_id TEXT, inclusion_status TEXT,
                      eventhood TEXT, rejection_reasons_json TEXT)""")
        self.assertFalse(layers_available(db))
        db.close()

    def test_it_degrades_where_the_rule_reading_was_never_kept(self):
        # An older database has the column missing or empty, and comparing
        # against nothing reads as total agreement — the most flattering answer
        # available, arrived at by having no data.
        db = sqlite3.connect(self.fixture_path)
        db.execute("UPDATE processed_documents SET rule_eventhood=NULL")
        db.commit()
        db.close()
        body = self.client.get("/api/ingestion/layers").json()
        self.assertFalse(body["available"])
        self.assertIn("rule_eventhood", body["reason"])
