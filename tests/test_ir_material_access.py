import sqlite3
import unittest

from collector.ir_material_access import (
    ACCESS_REASON, ensure_schema, parse_material_links, store_gated_jobs,
)


class IRMaterialAccessTests(unittest.TestCase):
    def test_parses_visible_cards_and_preserves_missing_href(self):
        body = b'''<html>Please fill in the real information for review
        <div class="list-item"><p class="date">2026/08/03</p><h1 class="title">H1 2026 Results</h1>
        <div><a class="link"><img src="presentation.svg"> Presentation</a>
        <a href="https://static.example/report.pdf"><span>Financial Report</span></a></div></div>
        <div class="list-item"><p class="date">2023/12/31</p><h1 class="title">Old</h1>
        <div><a class="link">Presentation</a></div></div></html>'''
        items = parse_material_links(body)
        self.assertEqual(len(items), 3)
        self.assertEqual((items[0].published_at, items[0].title,
                          items[0].material_label, items[0].href),
                         ("2026-08-03", "H1 2026 Results", "Presentation", None))
        self.assertEqual(items[1].href, "https://static.example/report.pdf")

    def test_requires_access_review_marker(self):
        body = b'''<div class="list-item"><p class="date">2026/08/03</p>
        <h1 class="title">Results</h1><a>Presentation</a></div>'''
        self.assertEqual(parse_material_links(body), [])

    def test_gated_job_storage_is_idempotent_and_honors_backfill(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        body = b'''Please fill in the real information for review
        <div class="list-item"><p class="date">2026/08/03</p><h1 class="title">Results</h1>
        <a>Presentation</a><a href="https://example/report.pdf">Report</a></div>
        <div class="list-item"><p class="date">2023/12/31</p><h1 class="title">Old</h1>
        <a>Presentation</a></div>'''
        items = parse_material_links(body)
        first = store_gated_jobs(db, "https://example/ir", items, "2024-01-01", "2026-08-25T00:00:00+00:00")
        second = store_gated_jobs(db, "https://example/ir", items, "2024-01-01", "2026-08-25T01:00:00+00:00")
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(db.execute("""SELECT title,material_label,status,public_url,reason,last_seen_at
            FROM ir_material_access_jobs""").fetchone(),
            ("Results", "Presentation", "manual_review", None, ACCESS_REASON,
             "2026-08-25T01:00:00+00:00"))
        public = [type(items[0])(
            items[0].published_at, items[0].title, items[0].material_label,
            "https://example/presentation.pdf")]
        self.assertEqual(store_gated_jobs(
            db, "https://example/ir", public, "2024-01-01",
            "2026-08-25T02:00:00+00:00"), [])
        self.assertEqual(db.execute("""SELECT status,public_url FROM ir_material_access_jobs
            WHERE title='Results'""").fetchone(),
            ("public_url_available", "https://example/presentation.pdf"))
        db.close()


if __name__ == "__main__":
    unittest.main()
