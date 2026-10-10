import json
import sqlite3
import unittest
from datetime import date, datetime, timezone

from collector.yicai_search import ensure_due_runs, ensure_schema, exact_results, infer_date, parse_results


class YicaiSearchTests(unittest.TestCase):
    def test_parses_public_result_payload_and_filters_exact_entity(self):
        body = json.dumps({"status": 1, "results": {"docs": [
            {"title": "长鑫科技发布新产品", "desc": "长鑫科技消息", "creationDate": "08-24 22:39",
             "url": "/news/103331001.html"},
            {"title": "其他公司", "desc": "无关内容", "creationDate": "2024-03-28 18:30",
             "url": "/news/103000002.html"},
            {"title": "视频", "desc": "长鑫科技", "creationDate": "08-24 20:00",
             "url": "/video/103000003.html"},
        ]}}).encode()
        items, dates, raw_count, has_data = parse_results(body, "2026-08-25")
        self.assertEqual(raw_count, 3)
        self.assertTrue(has_data)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "https://www.yicai.com/news/103331001.html")
        self.assertEqual(items[0].published_at, "2026-08-24")
        self.assertEqual(len(exact_results(items, "长鑫科技")), 1)
        self.assertEqual(dates, ["2026-08-24", "2024-03-28", "2023-08-24"])

    def test_short_date_rolls_back_across_year_boundary(self):
        self.assertEqual(infer_date("12-31 23:00", date(2026, 1, 2)), date(2025, 12, 31))
        self.assertEqual(infer_date("2024-02-29 12:00", date(2026, 1, 2)), date(2024, 2, 29))
        self.assertIsNone(infer_date("13-40", date(2026, 1, 2)))

    def test_invalid_payload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid status/results"):
            parse_results(b'{"status":0,"results":{}}', "2026-08-25")

    def test_due_runs_start_from_zero_and_then_increment_daily(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        ensure_due_runs(db, now)
        self.assertEqual(db.execute(
            "SELECT term_id,page,anchor_date,mode FROM yicai_search_jobs ORDER BY term_id").fetchall(), [
                ("cxmt", 0, "2026-08-25", "backfill"),
                ("wuxi_apptec", 0, "2026-08-25", "backfill")])
        db.execute("UPDATE yicai_search_jobs SET status='done'")
        db.execute("UPDATE yicai_search_state SET last_completed_at=?", (now.isoformat(),))
        ensure_due_runs(db, datetime(2026, 8, 27, tzinfo=timezone.utc))
        self.assertEqual(db.execute(
            "SELECT count(*) FROM yicai_search_jobs WHERE mode='incremental'").fetchone()[0], 2)
        db.close()


if __name__ == "__main__":
    unittest.main()
