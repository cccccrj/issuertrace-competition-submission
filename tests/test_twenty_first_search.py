import base64
import json
import sqlite3
import unittest
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from collector.twenty_first_search import (
    CALLBACK, IV, decrypt_results, ensure_due_runs, ensure_schema, normalize_date,
    parse_jsonp, result_items,
)


class TwentyFirstSearchTests(unittest.TestCase):
    def test_jsonp_and_public_aes_format(self):
        key = b"0123456789abcdef"
        original = [{"title": "test"}]
        padder = PKCS7(128).padder()
        padded = padder.update(json.dumps(original).encode()) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        body = (CALLBACK + "(" + json.dumps({
            "status": 1, "list": base64.b64encode(encrypted).decode()
        }) + ");").encode()
        self.assertEqual(decrypt_results(parse_jsonp(body), key), original)

    def test_exact_same_origin_results_only_and_http_is_upgraded(self):
        term = "长鑫科技集团股份有限公司"
        items = [
            {"title": "长鑫上市", "description": term,
             "url": "http://m.21jingji.com/article/20260729/herald/a.html",
             "inputtime": "2026-07-29", "is_pay": 0},
            {"title": "只有简称", "description": "长鑫科技",
             "url": "https://m.21jingji.com/article/20260729/herald/b.html",
             "inputtime": "2026-07-29"},
            {"title": term, "description": "外域",
             "url": "https://example.com/article/20260729/c.html",
             "inputtime": "2026-07-29"},
        ]
        self.assertEqual(result_items(items, term), [(
            "https://m.21jingji.com/article/20260729/herald/a.html",
            "长鑫上市", "2026-07-29", "independent_news")])

    def test_date_falls_back_to_article_path(self):
        self.assertEqual(normalize_date("", "https://m.21jingji.com/article/20240102/a"),
                         "2024-01-02")
        self.assertIsNone(normalize_date("invalid", "https://example.com/"))

    def test_due_runs_seed_only_full_legal_names(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        ensure_due_runs(db, datetime(2026, 8, 25, tzinfo=timezone.utc))
        rows = db.execute("""SELECT s.search_term,j.mode,j.page FROM twenty_first_search_jobs j
            JOIN twenty_first_search_state s ON s.term_id=j.term_id
            ORDER BY s.term_id""").fetchall()
        self.assertEqual(rows, [
            ("长鑫科技集团股份有限公司", "backfill", 1),
            ("无锡药明康德新药开发股份有限公司", "backfill", 1),
        ])
        db.close()

    def test_existing_search_job_schema_gains_skip_counter(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE twenty_first_search_jobs (
            job_id TEXT PRIMARY KEY,run_id TEXT,term_id TEXT,page INTEGER,mode TEXT,
            status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,last_error TEXT,
            processed_at TEXT)""")
        ensure_schema(db)
        columns = {row[1] for row in db.execute(
            "PRAGMA table_info(twenty_first_search_jobs)")}
        self.assertIn("skipped_before_window", columns)
        db.close()


if __name__ == "__main__":
    unittest.main()
