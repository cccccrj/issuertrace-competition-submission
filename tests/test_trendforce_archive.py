import sqlite3
import unittest
from datetime import datetime, timezone

from collector.trendforce_archive import (
    ARCHIVE_URL, ensure_due_run, ensure_schema, page_url, parse_archive_page,
)


class TrendForceArchiveTests(unittest.TestCase):
    def test_parser_accepts_only_dated_same_host_details_and_strong_aliases(self):
        body = b'''<div class="list-items">
          <div class="list-item"><div><h3><a class="title-link" href="/presscenter/news/20260818-1.html"><strong>CXMT expands DRAM output</strong></a></h3><p>Estimate.</p></div></div>
          <div class="list-item"><div><h3><a class="title-link" href="/presscenter/news/20260817-2.html"><strong>Memory outlook</strong></a></h3><p>ChangXin Memory Technologies is discussed.</p></div></div>
          <div class="list-item"><div><h3><a class="title-link" href="/presscenter/news/20260816-3.html"><strong>Other vendor</strong></a></h3></div></div>
          <div class="list-item"><div><a class="title-link" href="https://outside.example/presscenter/news/20260815-4.html">CXMT outside</a></div></div>
          <div class="list-item"><div><a class="title-link" href="/presscenter/news/not-dated.html">CXMT malformed</a></div></div>
        </div>'''
        dates, items = parse_archive_page(body)
        self.assertEqual(dates, ["2026-08-18", "2026-08-17", "2026-08-16"])
        self.assertEqual([(item.url, item.published_at) for item in items], [
            ("https://www.trendforce.com/presscenter/news/20260818-1.html", "2026-08-18"),
            ("https://www.trendforce.com/presscenter/news/20260817-2.html", "2026-08-17"),
        ])

    def test_page_url_has_hard_guard(self):
        self.assertEqual(page_url(1), ARCHIVE_URL)
        self.assertEqual(page_url(2), ARCHIVE_URL + "?page=2")
        with self.assertRaises(ValueError):
            page_url(101)

    def test_due_run_is_weekly_after_completed_backfill(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        self.assertEqual(ensure_due_run(db, now), 1)
        self.assertEqual(ensure_due_run(db, now), 0)
        db.execute("UPDATE trendforce_archive_jobs SET status='done'")
        db.execute("""UPDATE trendforce_archive_state
            SET last_completed_at='2026-08-24T00:00:00+00:00'""")
        self.assertEqual(ensure_due_run(db, now), 0)
        self.assertEqual(ensure_due_run(db, datetime(2026, 9, 1, 1, tzinfo=timezone.utc)), 1)
        row = db.execute("SELECT mode,page FROM trendforce_archive_jobs ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(row, ("incremental", 1))
        db.close()


if __name__ == "__main__":
    unittest.main()
