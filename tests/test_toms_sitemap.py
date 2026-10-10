import sqlite3
import unittest
from datetime import datetime, timezone

from collector.toms_sitemap import (
    _update_progress, ensure_schema, index_due, parse_month_sitemap,
    parse_sitemap_index,
)


class TomsSitemapTests(unittest.TestCase):
    def test_index_filters_host_format_and_window(self):
        body = b'''<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://www.tomshardware.com/sitemap-2023-12.xml</loc></sitemap>
          <sitemap><loc>https://www.tomshardware.com/sitemap-2024-01.xml</loc></sitemap>
          <sitemap><loc>https://www.tomshardware.com/sitemap-2026-08.xml</loc></sitemap>
          <sitemap><loc>https://outside.example/sitemap-2025-01.xml</loc></sitemap>
          <sitemap><loc>https://www.tomshardware.com/sitemap-news.xml</loc></sitemap>
        </sitemapindex>'''
        self.assertEqual(parse_sitemap_index(body, "2024-01", "2026-08"), [
            ("2026-08", "https://www.tomshardware.com/sitemap-2026-08.xml"),
            ("2024-01", "https://www.tomshardware.com/sitemap-2024-01.xml"),
        ])

    def test_month_parser_accepts_only_strong_entity_slugs(self):
        body = b'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.tomshardware.com/pc-components/dram/cxmt-opens-new-fab</loc>
            <image:image xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
              <image:loc>https://cdn.example/cxmt-photo.jpg</image:loc>
            </image:image></url>
          <url><loc>https://www.tomshardware.com/tech-industry/changxin-memory-technologies-update</loc></url>
          <url><loc>https://www.tomshardware.com/tech-industry/changxin-city-update</loc></url>
          <url><loc>https://www.tomshardware.com/tech-industry/ymtc-update</loc></url>
          <url><loc>https://outside.example/tech-industry/cxmt-update</loc></url>
        </urlset>'''
        total, items = parse_month_sitemap(body, "2026-08")
        self.assertEqual(total, 5)
        self.assertEqual([item.url for item in items], [
            "https://www.tomshardware.com/pc-components/dram/cxmt-opens-new-fab",
            "https://www.tomshardware.com/tech-industry/changxin-memory-technologies-update",
        ])

    def test_weekly_index_cadence(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        self.assertTrue(index_due(db, now))
        db.execute("""UPDATE toms_sitemap_state SET
            last_index_fetched_at='2026-08-20T00:00:00+00:00' WHERE source_id='toms_hardware'""")
        self.assertFalse(index_due(db, now))
        self.assertTrue(index_due(db, datetime(2026, 8, 28, tzinfo=timezone.utc)))
        db.close()

    def test_progress_updates_before_completion(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        db.execute("INSERT INTO toms_sitemap_jobs(sitemap_month,url,mode,status) VALUES"
                   "('2026-08','https://www.tomshardware.com/sitemap-2026-08.xml','backfill','done'),"
                   "('2026-07','https://www.tomshardware.com/sitemap-2026-07.xml','backfill','pending')")
        db.execute("""INSERT INTO toms_detail_jobs
            (url,title,sitemap_month,document_type,mode,first_seen_at)
            VALUES('https://www.tomshardware.com/tech-industry/cxmt-update','x','2026-08',
                   'hardware_news','backfill','2026-08-25T00:00:00+00:00')""")
        _update_progress(db, "2026-08-25T00:00:00+00:00")
        self.assertEqual(db.execute("""SELECT last_completed_at,months_scanned,
            exact_candidates FROM toms_sitemap_state""").fetchone(), (None, 1, 1))
        db.close()


if __name__ == "__main__":
    unittest.main()
