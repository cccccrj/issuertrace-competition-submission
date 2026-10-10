import sqlite3
import unittest
from datetime import datetime, timezone

from collector.nbd_search import ensure_due_runs, ensure_schema, normalize_date, result_items


class NBDSearchTests(unittest.TestCase):
    def test_exact_legal_name_results_only(self):
        term = "长鑫科技集团股份有限公司"
        payload = {"data": {"totalHits": 2, "searchResults": [
            {"title": "长鑫科技IPO", "digest": f"发行人为{term}",
             "url": "http://www.nbd.com.cn/articles/2026-07-01/1.html",
             "publishTime": "2026-07-01 12:30:00", "flashType": 0},
            {"title": "长鑫新闻", "digest": "只有简称",
             "url": "https://www.nbd.com.cn/articles/2026-07-01/2.html",
             "publishTime": "2026-07-01", "flashType": 2},
        ]}}
        total, items = result_items(payload, term)
        self.assertEqual(total, 2)
        self.assertEqual(items, [(
            "https://www.nbd.com.cn/articles/2026-07-01/1.html", "长鑫科技IPO",
            "2026-07-01", "independent_news")])

    def test_normalizes_epoch_and_rejects_invalid_date(self):
        self.assertEqual(normalize_date(1782864000000), "2026-07-01")
        self.assertEqual(normalize_date("2026/7/9 12:00"), "2026-07-09")
        self.assertIsNone(normalize_date("not a date"))

    def test_due_runs_use_full_legal_names_and_persistent_mode(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        rows = db.execute("""SELECT s.search_term,j.mode,j.result_offset FROM nbd_search_jobs j
            JOIN nbd_search_state s ON s.term_id=j.term_id ORDER BY s.term_id""").fetchall()
        self.assertEqual(rows, [
            ("长鑫科技集团股份有限公司", "backfill", 0),
            ("无锡药明康德新药开发股份有限公司", "backfill", 0),
        ])
        db.close()


if __name__ == "__main__":
    unittest.main()
