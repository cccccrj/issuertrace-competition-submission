import sqlite3
import unittest
from datetime import datetime, timezone

from collector.fierce_search import (
    ensure_due_runs, ensure_schema, normalize_date, result_items, search_url,
)


class FierceSearchTests(unittest.TestCase):
    def test_exact_current_domain_article_only(self):
        payload = {"meta": {"count": 2}, "data": [
            {"type": "node--article", "attributes": {
                "domain": ",fiercebiotech_com,", "title": "WuXi update",
                "field_date_published": "2026-07-01T09:00:00-04:00",
                "body": {"processed": "WuXi AppTec announced a deal"},
                "metatag": [{"tag": "link", "attributes": {
                    "rel": "canonical", "href": "https://www.fiercebiotech.com/biotech/wuxi-deal"}}],
            }},
            {"type": "node--article", "attributes": {
                "domain": ",fiercepharma_com,", "title": "WuXi AppTec other site",
                "field_date_published": "2026-07-01T09:00:00-04:00",
                "path": {"alias": "/manufacturing/wuxi"},
            }},
        ]}
        total, dates, items = result_items(
            payload, "WuXi AppTec", "www.fiercebiotech.com", "fiercebiotech_com")
        self.assertEqual(total, 2)
        self.assertEqual(dates, ["2026-07-01"])
        self.assertEqual(items, [(
            "https://www.fiercebiotech.com/biotech/wuxi-deal", "WuXi update",
            "2026-07-01", "industry_news")])

    def test_search_url_is_dated_sort_and_domain_scoped(self):
        url = search_url("fierce_pharma", 3)
        self.assertIn("page%5Blimit%5D=10", url)
        self.assertIn("page%5Boffset%5D=20", url)
        self.assertIn("filter%5Bfulltext%5D=WuXi+AppTec", url)
        self.assertIn("filter%5Bfield_domain_access%5D=fiercepharma_com", url)
        self.assertIn("sort%5Bfield_date_published%5D%5Bdirection%5D=desc", url)

    def test_date_normalization(self):
        self.assertEqual(normalize_date("2024-01-02T10:30:00-05:00"), "2024-01-02")
        self.assertIsNone(normalize_date("unknown"))

    def test_due_runs_cover_both_sites(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc),
                        {"fierce_biotech", "fierce_pharma"})
        self.assertEqual(db.execute("""SELECT source_id,mode,page FROM fierce_search_jobs
            ORDER BY source_id""").fetchall(), [
            ("fierce_biotech", "backfill", 1),
            ("fierce_pharma", "backfill", 1),
        ])
        db.close()


if __name__ == "__main__":
    unittest.main()
