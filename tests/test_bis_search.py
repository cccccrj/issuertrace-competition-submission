import sqlite3
import unittest

from collector.bis_search import mark_search_response, result_items, search_url, suspend_pending_jobs


class BISSearchTests(unittest.TestCase):
    def test_result_parser_accepts_only_exact_same_origin_hits(self):
        payload = [
            {"_source": {
                "title": ["Export controls involving CXMT"],
                "body": ["Guidance concerning CXMT and the Entity List."],
                "url": ["/regulations/example"],
            }},
            {"_source": {
                "title": "Unrelated content", "body": "No company identifier",
                "url": "/about-bis/example",
            }},
            {"_source": {
                "title": "CXMT mirror", "body": "CXMT",
                "url": "https://example.com/not-official",
            }},
        ]
        self.assertEqual(result_items(payload, "CXMT"), [(
            "https://www.bis.gov/regulations/example",
            "Export controls involving CXMT", "entity_list",
        )])

    def test_result_parser_preserves_valid_zero_result(self):
        self.assertEqual(result_items([], "WuXi AppTec"), [])
        with self.assertRaises(ValueError):
            result_items({"hits": []}, "WuXi AppTec")

    def test_search_url_uses_public_same_origin_endpoint(self):
        url = search_url("WuXi AppTec")
        self.assertEqual(url.split("?", 1)[0], "https://www.bis.gov/api/es-search")
        self.assertIn("query=WuXi+AppTec", url)
        self.assertIn("searchCategory=all", url)

    def test_access_restriction_suspends_pending_jobs(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE bis_search_state(term_id TEXT,last_error TEXT)")
        db.execute("CREATE TABLE bis_search_jobs(term_id TEXT,status TEXT,last_error TEXT)")
        db.executemany("INSERT INTO bis_search_state VALUES(?,NULL)", [("a",), ("b",)])
        db.executemany("INSERT INTO bis_search_jobs VALUES(?,?,NULL)",
                       [("a", "done"), ("b", "pending")])
        self.assertEqual(suspend_pending_jobs(db, "challenge"), 1)
        self.assertEqual(db.execute(
            "SELECT status,last_error FROM bis_search_jobs WHERE term_id='b'").fetchone(),
            ("manual_review", "challenge"))
        self.assertEqual(db.execute(
            "SELECT last_error FROM bis_search_state WHERE term_id='b'").fetchone()[0],
            "challenge")
        db.close()

    def test_query_response_is_excluded_even_after_not_modified(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE documents(
            source_id TEXT,canonical_url TEXT,review_status TEXT)""")
        db.execute("""CREATE TABLE document_versions(
            source_id TEXT,canonical_url TEXT,review_status TEXT,document_type TEXT)""")
        url = search_url("CXMT")
        db.execute("INSERT INTO documents VALUES('us_bis',?,'candidate_entity_match')", (url,))
        db.execute("""INSERT INTO document_versions VALUES(
            'us_bis',?,'candidate_entity_match','export_controls')""", (url,))
        mark_search_response(db, url)
        self.assertEqual(db.execute(
            "SELECT review_status FROM documents").fetchone()[0], "search_response")
        self.assertEqual(db.execute(
            "SELECT review_status,document_type FROM document_versions").fetchone(),
            ("search_response", "search_response"))
        db.close()


if __name__ == "__main__":
    unittest.main()
