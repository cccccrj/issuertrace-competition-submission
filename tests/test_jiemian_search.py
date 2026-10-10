import sqlite3
import unittest
from datetime import datetime, timezone

from collector.jiemian_search import (
    ensure_due_runs, ensure_schema, exact_results, next_detail_job, next_search_job, parse_results, search_url,
    should_prioritize_search,
)


SAMPLE = b"""
<div class="news-view left">
  <div class="news-img"><a href="https://www.jiemian.com/article/14977302.html"></a></div>
  <div class="news-right"><div class="news-header"><h3>
    <a href="https://www.jiemian.com/article/14977302.html" title="&#38271;&#37995;&#31185;&#25216;&#20170;&#26085;&#22823;&#23447;&#20132;&#26131;">hit</a>
  </h3></div><div class="news-main"><p>&#25104;&#20132;&#39069;240&#19975;&#20803;</p></div>
  <div class="news-footer"><span class="date">2026/08/24 17:30</span></div></div>
</div>
<div class="news-view left">
  <h3><a href="https://www.jiemian.com/article/14970000.html" title="&#20854;&#20182;&#20844;&#21496;">other</a></h3>
  <div class="news-main"><p>&#26080;&#20851;&#20869;&#23481;</p></div>
  <span class="date">2026/08/23 08:00</span>
</div><!--&#20998;&#39029;-->
"""


class JiemianSearchTests(unittest.TestCase):
    def test_parses_public_html_cards_and_exact_entity(self):
        items = parse_results(SAMPLE)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "https://www.jiemian.com/article/14977302.html")
        self.assertEqual(items[0].published_at, "2026-08-24")
        self.assertEqual([item.title for item in exact_results(items, "长鑫科技")],
                         ["长鑫科技今日大宗交易"])

    def test_rejects_invalid_dates_and_external_article_hosts(self):
        body = b"""
        <div class="news-view left"><h3><a href="https://example.com/article/1.html"
        title="bad">bad</a></h3><span class="date">2026/08/24</span></div>
        <div class="news-view left"><h3><a href="https://www.jiemian.com/article/2.html"
        title="bad date">bad</a></h3><span class="date">2026/13/40</span></div><!--&#20998;&#39029;-->
        """
        self.assertEqual(parse_results(body), [])

    def test_query_is_plain_get_with_bounded_page(self):
        self.assertEqual(search_url("药明康德", 2),
                         "https://a.jiemian.com/index.php?m=search&a=index&msg="
                         "%E8%8D%AF%E6%98%8E%E5%BA%B7%E5%BE%B7&opt=old&page=2")

    def test_due_runs_seed_backfill_then_daily_incremental(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        ensure_due_runs(db, now)
        self.assertEqual(db.execute(
            "SELECT term_id,mode,page FROM jiemian_search_jobs ORDER BY term_id").fetchall(), [
                ("cxmt", "backfill", 1), ("wuxi_apptec", "backfill", 1)])
        db.execute("UPDATE jiemian_search_jobs SET status='done'")
        db.execute("UPDATE jiemian_search_state SET last_completed_at=?", (now.isoformat(),))
        ensure_due_runs(db, now)
        self.assertEqual(db.execute("SELECT count(*) FROM jiemian_search_jobs").fetchone()[0], 2)
        ensure_due_runs(db, datetime(2026, 8, 27, tzinfo=timezone.utc))
        self.assertEqual(db.execute(
            "SELECT count(*) FROM jiemian_search_jobs WHERE mode='incremental'").fetchone()[0], 2)
        db.close()

    def test_less_scanned_company_is_selected_first(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        db.execute("UPDATE jiemian_search_state SET pages_scanned=3 WHERE term_id='cxmt'")
        self.assertEqual(next_search_job(db)[2], "wuxi_apptec")
        db.close()

    def test_explicit_focus_overrides_fair_backfill_order(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        db.execute("UPDATE jiemian_search_state SET pages_scanned=3 WHERE term_id='cxmt'")
        self.assertEqual(next_search_job(db, 'cxmt')[2], 'cxmt')
        db.close()

    def test_search_is_prioritized_after_a_detail_to_prevent_backlog_starvation(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        db.execute("UPDATE jiemian_search_jobs SET status='done',processed_at='2026-08-25T00:00:00+00:00'")
        db.execute("""INSERT INTO jiemian_detail_jobs(url,term_id,company_id,title,published_at,
            document_type,mode,first_seen_at,status,processed_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ('https://www.jiemian.com/article/1.html','cxmt','cxmt','x','2026-08-25',
             'independent_news','backfill','2026-08-25T00:00:00+00:00','done','2026-08-25T00:01:00+00:00'))
        db.execute("""INSERT INTO jiemian_search_jobs(job_id,run_id,term_id,page,mode)
            VALUES('next','next','wuxi_apptec',2,'backfill')""")
        self.assertTrue(should_prioritize_search(db))
        db.close()

    def test_detail_selection_prioritizes_less_served_entity(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        for url, term, status, published in [
            ('https://www.jiemian.com/article/done.html', 'cxmt', 'done', '2026-08-20'),
            ('https://www.jiemian.com/article/cxmt.html', 'cxmt', 'pending', '2026-08-26'),
            ('https://www.jiemian.com/article/wuxi.html', 'wuxi_apptec', 'pending', '2024-01-01'),
        ]:
            db.execute("""INSERT INTO jiemian_detail_jobs(url,term_id,company_id,title,published_at,
                document_type,mode,first_seen_at,status) VALUES(?,?,?,?,?,?,?,?,?)""",
                (url, term, term, 'x', published, 'independent_news', 'backfill',
                 '2026-08-25T00:00:00+00:00', status))
        self.assertEqual(next_detail_job(db)[1], 'wuxi_apptec')
        self.assertEqual(next_detail_job(db, 'cxmt')[1], 'cxmt')
        db.close()


if __name__ == "__main__":
    unittest.main()
