import json
import sqlite3
import unittest
from datetime import datetime, timezone

from collector.cnstock_search import (
    ensure_due_runs, ensure_schema, exact_results, next_detail_job, next_search_job, parse_results,
    should_prioritize_search,
)


class CnstockSearchTests(unittest.TestCase):
    def test_parses_public_search_payload_and_filters_exact_entity(self):
        body = json.dumps({
            "code": 200,
            "data": {
                "total": "21",
                "hasNext": True,
                "list": [
                    {
                        "contId": "770092",
                        "forwardType": 4,
                        "name": "长鑫科技发布新产品",
                        "summary": "<font>长鑫科技</font>相关报道",
                        "content": "<body><p>公开报道正文</p></body>",
                        "nodeInfo": {"name": "聚焦"},
                        "shareInfo": {"dateInfo": {
                            "year": 2026, "month": "08", "day": "25",
                        }},
                    },
                    {
                        "contId": "770091",
                        "forwardType": 4,
                        "name": "其他公司新闻",
                        "summary": "无关内容",
                        "content": "无关正文",
                        "nodeInfo": {"name": "快讯"},
                        "shareInfo": {"dateInfo": {
                            "year": 2026, "month": "08", "day": "24",
                        }},
                    },
                ],
            },
        }, ensure_ascii=False).encode()
        total, items, has_next, observed_dates = parse_results(body)
        self.assertEqual(total, 21)
        self.assertTrue(has_next)
        self.assertEqual(observed_dates, ["2026-08-25", "2026-08-24"])
        self.assertEqual(items[0].url, "https://www.cnstock.com/commonDetail/770092")
        self.assertEqual(items[1].document_type, "news_brief")
        self.assertEqual(exact_results(items, "长鑫科技"), [items[0]])

    def test_rejects_non_object_and_oversized_payloads(self):
        with self.assertRaisesRegex(ValueError, "invalid code/data"):
            parse_results(b"[]")
        oversized = {"code": 200, "data": {
            "total": 21, "hasNext": True, "list": [{} for _ in range(21)],
        }}
        with self.assertRaisesRegex(ValueError, "oversized list"):
            parse_results(json.dumps(oversized).encode())

    def test_due_runs_seed_two_backfills_then_daily_incrementals(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        ensure_due_runs(db, now)
        self.assertEqual(db.execute(
            "SELECT term_id,page,mode FROM cnstock_search_jobs ORDER BY term_id").fetchall(), [
                ("cxmt", 1, "backfill"),
                ("wuxi_apptec", 1, "backfill"),
            ])
        db.execute("UPDATE cnstock_search_jobs SET status='done'")
        db.execute("UPDATE cnstock_search_state SET last_completed_at=?", (now.isoformat(),))
        ensure_due_runs(db, now)
        self.assertEqual(db.execute("SELECT count(*) FROM cnstock_search_jobs").fetchone()[0], 2)
        ensure_due_runs(db, datetime(2026, 8, 27, tzinfo=timezone.utc))
        self.assertEqual(db.execute(
            "SELECT count(*) FROM cnstock_search_jobs WHERE mode='incremental'").fetchone()[0], 2)
        db.close()

    def test_less_scanned_company_is_selected_first(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        db.execute("UPDATE cnstock_search_state SET pages_scanned=3 WHERE term_id='cxmt'")
        self.assertEqual(next_search_job(db)[2], "wuxi_apptec")
        db.close()

    def test_explicit_focus_overrides_fair_backfill_order(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        db.execute("UPDATE cnstock_search_state SET pages_scanned=3 WHERE term_id='cxmt'")
        self.assertEqual(next_search_job(db, 'cxmt')[2], 'cxmt')
        db.close()

    def test_search_is_prioritized_after_a_detail_to_prevent_backlog_starvation(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        db.execute("UPDATE cnstock_search_jobs SET status='done',processed_at='2026-08-25T00:00:00+00:00'")
        db.execute("""INSERT INTO cnstock_detail_jobs(url,term_id,company_id,title,published_at,
            document_type,mode,first_seen_at,status,processed_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ('https://www.cnstock.com/commonDetail/1','cxmt','cxmt','x','2026-08-25',
             'news','backfill','2026-08-25T00:00:00+00:00','done','2026-08-25T00:01:00+00:00'))
        db.execute("""INSERT INTO cnstock_search_jobs(job_id,run_id,term_id,page,mode)
            VALUES('next','next','wuxi_apptec',2,'backfill')""")
        self.assertTrue(should_prioritize_search(db))
        db.close()

    def test_detail_selection_prioritizes_less_served_entity(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        for url, term, status, published in [
            ('https://www.cnstock.com/commonDetail/done', 'cxmt', 'done', '2026-08-20'),
            ('https://www.cnstock.com/commonDetail/cxmt', 'cxmt', 'pending', '2026-08-26'),
            ('https://www.cnstock.com/commonDetail/wuxi', 'wuxi_apptec', 'pending', '2024-01-01'),
        ]:
            db.execute("""INSERT INTO cnstock_detail_jobs(url,term_id,company_id,title,published_at,
                document_type,mode,first_seen_at,status) VALUES(?,?,?,?,?,?,?,?,?)""",
                (url, term, term, 'x', published, 'news', 'backfill',
                 '2026-08-25T00:00:00+00:00', status))
        self.assertEqual(next_detail_job(db)[1], 'wuxi_apptec')
        self.assertEqual(next_detail_job(db, 'cxmt')[1], 'cxmt')
        db.close()


if __name__ == "__main__":
    unittest.main()
