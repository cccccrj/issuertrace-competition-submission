import sqlite3
import unittest

from collector.openfda_enforcement import (
    exact_items, mark_search_response, parse_payload, search_url, suspend_pending_jobs,
)
from collector.collect import canonicalize


class OpenFDAEnforcementTests(unittest.TestCase):
    def test_not_found_payload_is_a_valid_zero_result(self):
        payload = {"error": {"code": "NOT_FOUND", "message": "No matches found!"}}
        self.assertEqual(parse_payload(404, payload), ([], 0))

    def test_success_payload_requires_results_metadata(self):
        payload = {"meta": {"results": {"total": 1}},
                   "results": [{"recalling_firm": "WuXi AppTec Co., Ltd."}]}
        self.assertEqual(parse_payload(200, payload), (payload["results"], 1))
        with self.assertRaises(ValueError):
            parse_payload(200, {"results": []})

    def test_exact_items_rejects_unrelated_wuxi_place_name(self):
        items = [
            {"recalling_firm": "WuXi AppTec Co., Ltd."},
            {"recalling_firm": "Wuxi Medical Instrument Factory"},
        ]
        self.assertEqual(exact_items(items, "WuXi AppTec"), [items[0]])

    def test_search_url_has_endpoint_phrase_date_range_and_cap(self):
        url = search_url("drug", "WuXi AppTec", "2024-01-01", "2026-08-25")
        self.assertTrue(url.startswith("https://api.fda.gov/drug/enforcement.json?"))
        self.assertIn("recalling_firm%3A%22WuXi+AppTec%22", url)
        self.assertIn("report_date%3A%5B20240101+TO+20260825%5D", url)
        self.assertIn("limit=1000", url)

    def test_access_restriction_suspends_pending_jobs(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE openfda_enforcement_state(term_id TEXT,last_error TEXT)")
        db.execute("CREATE TABLE openfda_enforcement_jobs(term_id TEXT,status TEXT,last_error TEXT)")
        db.executemany("INSERT INTO openfda_enforcement_state VALUES(?,NULL)", [("a",), ("b",)])
        db.executemany("INSERT INTO openfda_enforcement_jobs VALUES(?,?,NULL)",
                       [("a", "done"), ("b", "pending")])
        self.assertEqual(suspend_pending_jobs(db, "HTTP 403"), 1)
        self.assertEqual(db.execute(
            "SELECT status,last_error FROM openfda_enforcement_jobs WHERE term_id='b'"
        ).fetchone(), ("manual_review", "HTTP 403"))
        db.close()

    def test_query_response_is_excluded_from_scope_coverage(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE documents(source_id TEXT,canonical_url TEXT,review_status TEXT)")
        db.execute("""CREATE TABLE document_versions(
            source_id TEXT,canonical_url TEXT,review_status TEXT,document_type TEXT)""")
        url = search_url("device", "WuXi STA", "2024-01-01", "2026-08-25")
        normalized = canonicalize(url)
        db.execute("INSERT INTO documents VALUES('us_fda_openfda',?,'candidate_entity_match')", (normalized,))
        db.execute("INSERT INTO document_versions VALUES('us_fda_openfda',?,'candidate_entity_match','recalls')", (normalized,))
        mark_search_response(db, url)
        self.assertEqual(db.execute("SELECT review_status FROM documents").fetchone()[0],
                         "search_response")
        self.assertEqual(db.execute(
            "SELECT review_status,document_type FROM document_versions").fetchone(),
            ("search_response", "search_response"))
        db.close()


if __name__ == "__main__":
    unittest.main()
