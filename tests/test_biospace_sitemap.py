import sqlite3
import unittest
from datetime import datetime, timezone

from collector.biospace_sitemap import (
    _finish_state_if_complete, ensure_schema, index_fetch_due, parse_detail_metadata, parse_month_sitemap,
    parse_sitemap_index,
)


class BioSpaceSitemapTests(unittest.TestCase):
    def test_index_accepts_only_registered_monthly_sitemaps_in_window(self):
        body = b'''<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://www.biospace.com/sitemap-202312.xml</loc></sitemap>
          <sitemap><loc>https://www.biospace.com/sitemap-202401.xml</loc></sitemap>
          <sitemap><loc>https://www.biospace.com/sitemap-202608.xml</loc></sitemap>
          <sitemap><loc>https://outside.example/sitemap-202501.xml</loc></sitemap>
          <sitemap><loc>https://www.biospace.com/sitemap-latest.xml</loc></sitemap>
        </sitemapindex>'''
        self.assertEqual(parse_sitemap_index(body, "2024-01", "2026-08"), [
            ("2026-08", "https://www.biospace.com/sitemap-202608.xml"),
            ("2024-01", "https://www.biospace.com/sitemap-202401.xml"),
        ])

    def test_month_parser_requires_exact_wuxi_apptec_slug(self):
        body = b'''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.biospace.com/press-releases/wuxi-apptec-joins-initiative</loc>
            <image:image xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
              <image:loc>https://cdn.example/wuxi-apptec-photo.jpg</image:loc>
            </image:image></url>
          <url><loc>https://www.biospace.com/business/namsa-acquires-unit-of-wuxi-apptec</loc></url>
          <url><loc>https://www.biospace.com/business/wuxi-biologics-opens-site</loc></url>
          <url><loc>https://www.biospace.com/business/wuxi-city-update</loc></url>
          <url><loc>https://outside.example/business/wuxi-apptec-story</loc></url>
        </urlset>'''
        total, items = parse_month_sitemap(body, "2025-01")
        self.assertEqual(total, 5)
        self.assertEqual([(item.url, item.sitemap_month, item.document_type)
                          for item in items], [
            ("https://www.biospace.com/press-releases/wuxi-apptec-joins-initiative",
             "2025-01", "press_releases"),
            ("https://www.biospace.com/business/namsa-acquires-unit-of-wuxi-apptec",
             "2025-01", "industry_news"),
        ])

    def test_detail_metadata_uses_public_article_fields(self):
        body = b'''<html><head>
          <meta property="og:title" content="WuXi AppTec &amp; Partner Update">
          <meta property="article:published_time" content="2025-01-17T09:39:22-05:00">
        </head></html>'''
        self.assertEqual(parse_detail_metadata(body),
                         ("WuXi AppTec & Partner Update", "2025-01-17"))

    def test_an_apostrophe_does_not_end_the_title(self):
        # I-05: content="China's banned memory-maker…" was stored as "China".
        body = b'''<html><head>
          <meta property="og:title" content="China's banned memory-maker CXMT unveils DDR5-8000">
          <meta property="article:published_time" content="2025-11-26T09:00:00Z">
        </head></html>'''
        self.assertEqual(parse_detail_metadata(body)[0], "China's banned memory-maker CXMT unveils DDR5-8000")

    def test_a_single_quoted_title_keeps_its_double_quotes(self):
        body = b'''<html><head>
          <meta property='og:title' content='Three PC makers fight "unprecedented" shortages'>
          <meta property="article:published_time" content="2025-11-26T09:00:00Z">
        </head></html>'''
        self.assertEqual(parse_detail_metadata(body)[0], 'Three PC makers fight "unprecedented" shortages')

    def test_weekly_index_cadence(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        self.assertTrue(index_fetch_due(db, now))
        db.execute("""UPDATE biospace_sitemap_state
            SET last_index_fetched_at='2026-08-20T00:00:00+00:00'
            WHERE source_id='biospace'""")
        self.assertFalse(index_fetch_due(db, now))
        self.assertTrue(index_fetch_due(db, datetime(2026, 8, 28, tzinfo=timezone.utc)))
        db.close()

    def test_progress_counts_update_before_all_months_complete(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        db.execute("INSERT INTO biospace_sitemap_jobs(sitemap_month,url,mode,status) VALUES"
                   "('2026-08','https://www.biospace.com/sitemap-202608.xml','backfill','done'),"
                   "('2026-07','https://www.biospace.com/sitemap-202607.xml','backfill','pending')")
        db.execute("""INSERT INTO biospace_detail_jobs
            (url,title,sitemap_month,document_type,mode,first_seen_at)
            VALUES('https://www.biospace.com/business/wuxi-apptec-update','x','2026-08',
                   'industry_news','backfill','2026-08-25T00:00:00+00:00')""")
        _finish_state_if_complete(db, "2026-08-25T00:00:00+00:00")
        row = db.execute("""SELECT last_completed_at,months_scanned,exact_candidates
            FROM biospace_sitemap_state WHERE source_id='biospace'""").fetchone()
        self.assertEqual(row, (None, 1, 1))
        db.close()


if __name__ == "__main__":
    unittest.main()
