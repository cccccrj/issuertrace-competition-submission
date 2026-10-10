"""Element standardisation over the API — 命题主要任务第 2 项.

The extraction half of the task has always been visible; the standardisation
half was not. What these check is that the endpoint reports the part that is not
flattering: a per-type resolution rate, so a type resolving at 41% cannot hide
behind an overall figure of 88%.
"""
from __future__ import annotations

import sqlite3
import unittest

from tests.test_api import ApiTestCase


class ElementTestCase(ApiTestCase):
    def seed(self, rows=None):
        db = sqlite3.connect(self.fixture_path)
        db.execute("""CREATE TABLE IF NOT EXISTS event_elements(
            element_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, element_type TEXT NOT NULL,
            ordinal INTEGER NOT NULL, raw_text TEXT NOT NULL, normalized_value TEXT,
            unit TEXT, basis TEXT, status TEXT NOT NULL, extra_json TEXT NOT NULL DEFAULT '{}')""")
        rows = rows if rows is not None else [
            # Amounts resolve well…
            ("el-1", "e1", "amount", 0, "295亿元", "29500000000", "CNY", "explicit", "normalized"),
            ("el-2", "e1", "amount", 1, "百亿级", None, None, None, "unresolved"),
            # …subjects much less so, which is the finding the page exists for.
            ("el-3", "e1", "subject", 2, "长鑫科技", "cxmt", "company_id", None, "normalized"),
            ("el-4", "e1", "subject", 3, "某上游供应商", None, None, None, "unresolved"),
            ("el-5", "e1", "subject", 4, "业内人士", None, None, None, "unresolved"),
            # A time normalised by inference rather than from the text.
            ("el-6", "e1", "time", 5, "近日", "2026-08-01", "day", "relative_to_publication",
             "normalized"),
        ]
        db.executemany("""INSERT OR REPLACE INTO event_elements
            (element_id, event_id, element_type, ordinal, raw_text, normalized_value,
             unit, basis, status) VALUES(?,?,?,?,?,?,?,?,?)""", rows)
        db.commit()
        db.close()


class StatsTests(ElementTestCase):
    def test_the_resolution_rate_is_reported_per_type_not_only_overall(self):
        # One aggregate rate would let a type resolving at a third hide behind
        # the types that resolve completely.
        self.seed()
        by_type = {row["element_type"]: row for row in
                   self.client.get("/api/elements/stats").json()["by_type"]}
        # Rates come back rounded to four places, which is plenty for a rate and
        # keeps the JSON readable.
        self.assertAlmostEqual(by_type["amount"]["normalized_rate"], 0.5, places=4)
        self.assertAlmostEqual(by_type["subject"]["normalized_rate"], 1 / 3, places=4)
        self.assertEqual(by_type["subject"]["unresolved"], 2)

    def test_types_are_ordered_by_volume_so_the_table_reads_consistently(self):
        self.seed()
        totals = [row["total"] for row in self.client.get("/api/elements/stats").json()["by_type"]]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_the_overall_rate_counts_every_element(self):
        self.seed()
        totals = self.client.get("/api/elements/stats").json()["totals"]
        self.assertEqual(totals["elements"], 6)
        self.assertEqual(totals["normalized"], 3)
        self.assertAlmostEqual(totals["normalized_rate"], 0.5)

    def test_the_basis_of_a_normalised_value_is_reported(self):
        # A time read off the page and a time inferred from the publication date
        # are both "normalized"; they are not equally strong facts.
        self.seed()
        basis = {row["basis"]: row["count"]
                 for row in self.client.get("/api/elements/stats").json()["by_basis"]}
        self.assertEqual(basis["relative_to_publication"], 1)
        self.assertEqual(basis["explicit"], 1)
        self.assertEqual(basis["未记录"], 4)

    def test_units_are_listed_for_the_values_that_have_them(self):
        self.seed()
        units = {row["unit"] for row in self.client.get("/api/elements/stats").json()["by_unit"]}
        self.assertEqual(units, {"CNY", "company_id", "day"})

    def test_an_empty_type_does_not_divide_by_zero(self):
        self.seed(rows=[])
        totals = self.client.get("/api/elements/stats").json()["totals"]
        self.assertEqual(totals["elements"], 0)
        self.assertIsNone(totals["normalized_rate"])


class ListTests(ElementTestCase):
    def test_an_unresolved_element_keeps_its_raw_text(self):
        # Not resolving is not the same as having nothing: the original wording
        # is the evidence, and it stays.
        self.seed()
        items = self.client.get("/api/elements", params={"status": "unresolved"}).json()["items"]
        self.assertEqual(len(items), 3)
        self.assertTrue(all(row["raw_text"] for row in items))
        self.assertTrue(all(row["normalized_value"] is None for row in items))

    def test_the_type_filter_narrows(self):
        self.seed()
        body = self.client.get("/api/elements", params={"element_type": "subject"}).json()
        self.assertEqual(body["total"], 3)

    def test_search_matches_the_raw_text_and_the_normalised_value(self):
        self.seed()
        self.assertEqual(self.client.get("/api/elements", params={"search": "295亿"}).json()["total"], 1)
        self.assertEqual(self.client.get("/api/elements", params={"search": "cxmt"}).json()["total"], 1)

    def test_each_element_carries_the_event_it_came_from(self):
        self.seed()
        row = self.client.get("/api/elements", params={"element_type": "amount"}).json()["items"][0]
        self.assertEqual(row["event_id"], "e1")
        self.assertTrue(row["event_summary"])


class DegradationTests(ApiTestCase):
    def test_it_reports_unavailable_rather_than_failing_when_extraction_has_not_run(self):
        # A deployment whose pipeline has not reached this stage should see an
        # empty page saying so, not a 500.
        body = self.client.get("/api/elements/stats").json()
        self.assertFalse(body["available"])
        self.assertEqual(self.client.get("/api/elements").json()["items"], [])


if __name__ == "__main__":
    unittest.main()
