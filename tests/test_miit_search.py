import sqlite3
import unittest

from collector.miit_search import published_date, result_items, search_url, suspend_pending_jobs


class MIITSearchTests(unittest.TestCase):
    def test_result_parser_accepts_only_miit_entity_hits(self):
        payload = {"success": True, "code": "200", "data": {"searchResult": {
            "total": 1, "dataResults": [{"groupData": [{"data": {
                "title_text": "<em>长鑫存储</em>入选示范名单",
                "infocontent": "长鑫存储相关信息",
                "deploytime": "1706501917326",
                "url": "/zwgk/art/2024/example.html",
            }}]}],
        }}}
        items, total = result_items(payload, "长鑫存储")
        self.assertEqual(total, 1)
        self.assertEqual(items, [(
            "https://www.miit.gov.cn/zwgk/art/2024/example.html",
            "长鑫存储入选示范名单", "2024-01-29", "industry_list",
        )])

    def test_result_parser_rejects_unsuccessful_payload(self):
        with self.assertRaises(ValueError):
            result_items({"success": False, "code": "500", "message": "失败"}, "CXMT")

    def test_search_url_has_date_bounds_and_official_category(self):
        url = search_url("CXMT", 2, "2024-01-01", "2026-08-25")
        self.assertIn("cateid=54", url)
        self.assertIn("begin=2024-01-01", url)
        self.assertIn("end=2026-08-25", url)
        self.assertIn("p=2", url)

    def test_epoch_milliseconds_use_shanghai_date(self):
        self.assertEqual(published_date("1706501917326"), "2024-01-29")

    def test_access_challenge_suspends_all_pending_jobs(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE miit_search_state(term_id TEXT,last_error TEXT)")
        db.execute("""CREATE TABLE miit_search_jobs(
            term_id TEXT,status TEXT,last_error TEXT)""")
        db.executemany("INSERT INTO miit_search_state VALUES(?,NULL)", [("a",), ("b",)])
        db.executemany("INSERT INTO miit_search_jobs VALUES(?,?,NULL)",
                       [("a", "done"), ("b", "pending")])
        self.assertEqual(suspend_pending_jobs(db, "challenge"), 1)
        self.assertEqual(db.execute("SELECT status,last_error FROM miit_search_jobs WHERE term_id='b'").fetchone(),
                         ("manual_review", "challenge"))
        self.assertEqual(db.execute("SELECT last_error FROM miit_search_state WHERE term_id='b'").fetchone()[0],
                         "challenge")
        db.close()


if __name__ == "__main__":
    unittest.main()
