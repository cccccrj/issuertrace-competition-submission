import json
import sqlite3
import unittest
from datetime import date, datetime, timezone

from collector.stcn_search import (
    ensure_due_runs, ensure_schema, exact_results, infer_date, next_detail_job, next_search_job, parse_results,
    should_prioritize_search,
)


class StcnSearchTests(unittest.TestCase):
    def test_parses_frontend_json_and_filters_exact_entity(self):
        fragment = """
        <li class="no-pr"><div class="content"><div class="tt">
          <a href="/article/detail/4106127.html"><i>长鑫科技</i>大宗交易</a>
        </div><div class="text ellipsis-2"><a>成交额240万元</a></div>
        <div class="info"><span>数据宝</span><span>08-24 22:39</span></div></div></li>
        <li><div class="content"><div class="tt">
          <a href="/article/detail/4106000.html">其他公司</a>
        </div><div class="info"><span>证券时报</span><span>08-23 10:00</span></div></div></li>
        """
        body = json.dumps({"state": 1, "data": fragment, "page_time": 2}).encode()
        items, dates, next_page, has_data = parse_results(body, "2026-08-25")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].published_at, "2026-08-24")
        self.assertEqual(items[0].url, "https://www.stcn.com/article/detail/4106127.html")
        self.assertEqual(len(exact_results(items, "长鑫科技")), 1)
        self.assertEqual(dates, ["2026-08-24", "2026-08-23"])
        self.assertEqual(next_page, 2)
        self.assertTrue(has_data)

    def test_short_dates_roll_back_across_year_boundary(self):
        self.assertEqual(infer_date("12-31 23:00", date(2026, 1, 2)), date(2025, 12, 31))
        self.assertEqual(infer_date("2024-02-29 12:00", date(2026, 1, 2)), date(2024, 2, 29))
        self.assertIsNone(infer_date("13-40", date(2026, 1, 2)))

    def test_rejects_invalid_payload(self):
        with self.assertRaisesRegex(ValueError, "invalid state/data"):
            parse_results(b'{"state":0,"data":"","page_time":1}', "2026-08-25")

    def test_due_runs_seed_two_backfills_then_daily_incrementals(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        ensure_due_runs(db, now)
        self.assertEqual(db.execute(
            "SELECT term_id,page,anchor_date,mode FROM stcn_search_jobs ORDER BY term_id").fetchall(), [
                ("cxmt", 1, "2026-08-25", "backfill"),
                ("wuxi_apptec", 1, "2026-08-25", "backfill")])
        db.execute("UPDATE stcn_search_jobs SET status='done'")
        db.execute("UPDATE stcn_search_state SET last_completed_at=?", (now.isoformat(),))
        ensure_due_runs(db, now)
        self.assertEqual(db.execute("SELECT count(*) FROM stcn_search_jobs").fetchone()[0], 2)
        ensure_due_runs(db, datetime(2026, 8, 27, tzinfo=timezone.utc))
        self.assertEqual(db.execute(
            "SELECT count(*) FROM stcn_search_jobs WHERE mode='incremental'").fetchone()[0], 2)
        db.close()

    def test_less_scanned_company_is_selected_first(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        db.execute("UPDATE stcn_search_state SET pages_scanned=3 WHERE term_id='cxmt'")
        self.assertEqual(next_search_job(db)[2], "wuxi_apptec")
        db.close()

    def test_explicit_focus_overrides_fair_backfill_order(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        db.execute("UPDATE stcn_search_state SET pages_scanned=3 WHERE term_id='cxmt'")
        self.assertEqual(next_search_job(db, 'cxmt')[2], 'cxmt')
        db.close()

    def test_search_is_prioritized_after_a_detail_to_prevent_backlog_starvation(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        db.execute("UPDATE stcn_search_jobs SET status='done',processed_at='2026-08-25T00:00:00+00:00'")
        db.execute("""INSERT INTO stcn_detail_jobs(url,term_id,company_id,title,published_at,mode,
            first_seen_at,status,processed_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            ('https://www.stcn.com/article/detail/1.html','cxmt','cxmt','x','2026-08-25',
             'backfill','2026-08-25T00:00:00+00:00','done','2026-08-25T00:01:00+00:00'))
        db.execute("""INSERT INTO stcn_search_jobs(job_id,run_id,term_id,page,anchor_date,mode)
            VALUES('next','next','wuxi_apptec',2,'2026-08-25','backfill')""")
        self.assertTrue(should_prioritize_search(db))
        db.close()

    def test_detail_selection_prioritizes_less_served_entity(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        for url, term, status, published in [
            ('https://www.stcn.com/article/detail/done.html', 'cxmt', 'done', '2026-08-20'),
            ('https://www.stcn.com/article/detail/cxmt.html', 'cxmt', 'pending', '2026-08-26'),
            ('https://www.stcn.com/article/detail/wuxi.html', 'wuxi_apptec', 'pending', '2024-01-01'),
        ]:
            db.execute("""INSERT INTO stcn_detail_jobs(url,term_id,company_id,title,published_at,mode,
                first_seen_at,status) VALUES(?,?,?,?,?,?,?,?)""",
                (url, term, term, 'x', published, 'backfill', '2026-08-25T00:00:00+00:00', status))
        self.assertEqual(next_detail_job(db)[1], 'wuxi_apptec')
        self.assertEqual(next_detail_job(db, 'cxmt')[1], 'cxmt')
        db.close()


if __name__ == "__main__":
    unittest.main()
