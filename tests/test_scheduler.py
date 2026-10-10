import sqlite3
import unittest

from datetime import datetime, timezone

from collector.scheduler import (
    campaign_completion_progress, has_pending_adapter_backfill, has_pending_sse_backfill, has_runnable_frontier,
    maintenance_due, sleep_base_seconds,
)


class SchedulerTests(unittest.TestCase):
    def test_campaign_completion_gate_counts_only_latest_valid_target_types(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE documents(source_id TEXT,canonical_url TEXT)")
        db.execute("""CREATE TABLE document_versions(version_id TEXT,source_id TEXT,
            canonical_url TEXT,fetched_at TEXT,company_id TEXT,document_type TEXT,
            review_status TEXT)""")
        db.execute("INSERT INTO documents VALUES('news','https://example/a')")
        db.execute("INSERT INTO document_versions VALUES('old','news','https://example/a','2026-01-01','cxmt','independent_news','candidate_entity_match')")
        db.execute("INSERT INTO document_versions VALUES('new','news','https://example/a','2026-02-01','cxmt','independent_news','candidate_entity_match')")
        settings = {"completion_gate": {"company_id": "cxmt",
            "minimum_valid_documents": 1, "document_types": ["independent_news"],
            "stop_daemon": True}}
        self.assertEqual(campaign_completion_progress(db, settings), (1, 1))
        db.execute("UPDATE document_versions SET review_status='pending_entity_review' WHERE version_id='new'")
        self.assertEqual(campaign_completion_progress(db, settings), (0, 1))
        db.close()
    def test_catch_up_interval_only_applies_to_p0_backfill(self):
        self.assertEqual(sleep_base_seconds("ALL", 300, 60, True), 60)
        self.assertEqual(sleep_base_seconds("P0", 300, 60, True), 60)
        self.assertEqual(sleep_base_seconds("P1", 300, 60, True), 300)
        self.assertEqual(sleep_base_seconds("ALL", 300, 60, False), 300)
        self.assertEqual(sleep_base_seconds("P1", 300, 60, False, True), 60)

    def test_pending_incremental_jobs_do_not_keep_catch_up_mode(self):
        db = sqlite3.connect(":memory:")
        self.assertFalse(has_pending_sse_backfill(db))
        db.execute("""CREATE TABLE api_jobs (
            source_id TEXT, job_id TEXT, status TEXT)""")
        db.execute("INSERT INTO api_jobs VALUES('sse_announcements','incremental:cxmt:2026-08-25:1','pending')")
        self.assertFalse(has_pending_sse_backfill(db))
        db.execute("INSERT INTO api_jobs VALUES('sse_announcements','cxmt:2025-01-01:2025-01-31:1','pending')")
        self.assertTrue(has_pending_sse_backfill(db))
        db.close()

    def test_only_runnable_frontier_triggers_catch_up(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE crawl_frontier(source_id TEXT,status TEXT)")
        db.execute("CREATE TABLE source_state(source_id TEXT,state TEXT,cooldown_until TEXT)")
        db.execute("INSERT INTO crawl_frontier VALUES('healthy','pending')")
        db.execute("INSERT INTO crawl_frontier VALUES('blocked','pending')")
        db.execute("INSERT INTO source_state VALUES('blocked','manual_review',NULL)")
        self.assertTrue(has_runnable_frontier(db, ["healthy", "blocked"]))
        self.assertFalse(has_runnable_frontier(db, ["blocked"]))
        self.assertFalse(has_runnable_frontier(db, []))
        db.close()

    def test_storage_maintenance_obeys_persistent_interval(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE maintenance_state(job_id TEXT PRIMARY KEY,last_run_at TEXT,status TEXT,details TEXT)")
        now = datetime(2026, 8, 24, 22, 0, tzinfo=timezone.utc)
        self.assertTrue(maintenance_due(db, "verify_storage", 24, now))
        db.execute("INSERT INTO maintenance_state VALUES('verify_storage','2026-08-24T21:00:00+00:00','ok',NULL)")
        self.assertFalse(maintenance_due(db, "verify_storage", 24, now))
        self.assertTrue(maintenance_due(db, "verify_storage", 0, now) is False)
        self.assertTrue(maintenance_due(db, "verify_storage", 1, now))
        db.close()

    def test_bis_backfill_is_a_p0_adapter_backfill(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE bis_search_jobs(mode TEXT,status TEXT)")
        db.execute("INSERT INTO bis_search_jobs VALUES('backfill','pending')")
        self.assertTrue(has_pending_adapter_backfill(db, "P0"))
        self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
        self.assertFalse(has_pending_adapter_backfill(db, "P1"))
        db.close()

    def test_openfda_backfill_is_a_p0_adapter_backfill(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE openfda_enforcement_jobs(mode TEXT,status TEXT)")
        db.execute("INSERT INTO openfda_enforcement_jobs VALUES('backfill','pending')")
        self.assertTrue(has_pending_adapter_backfill(db, "P0"))
        self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
        self.assertFalse(has_pending_adapter_backfill(db, "P1"))
        db.close()

    def test_govinfo_packages_are_a_p0_adapter_backfill(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE govinfo_package_jobs(mode TEXT,status TEXT)")
        db.execute("INSERT INTO govinfo_package_jobs VALUES('backfill','pending')")
        self.assertTrue(has_pending_adapter_backfill(db, "P0"))
        self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
        self.assertFalse(has_pending_adapter_backfill(db, "P1"))
        db.close()

    def test_cfm_jobs_are_a_p1_adapter_backfill(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE cfm_producer_jobs(mode TEXT,status TEXT)")
        db.execute("INSERT INTO cfm_producer_jobs VALUES('backfill','pending')")
        self.assertFalse(has_pending_adapter_backfill(db, "P0"))
        self.assertTrue(has_pending_adapter_backfill(db, "P1"))
        self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
        db.close()

    def test_nbd_search_and_detail_are_p1_adapter_backfill(self):
        for table in ("nbd_search_jobs", "nbd_detail_jobs"):
            with self.subTest(table=table):
                db = sqlite3.connect(":memory:")
                db.execute(f"CREATE TABLE {table}(mode TEXT,status TEXT)")
                db.execute(f"INSERT INTO {table} VALUES('backfill','pending')")
                self.assertFalse(has_pending_adapter_backfill(db, "P0"))
                self.assertTrue(has_pending_adapter_backfill(db, "P1"))
                self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
                db.close()

    def test_twenty_first_search_and_detail_are_p1_adapter_backfill(self):
        for table in ("twenty_first_search_jobs", "twenty_first_detail_jobs"):
            with self.subTest(table=table):
                db = sqlite3.connect(":memory:")
                db.execute(f"CREATE TABLE {table}(mode TEXT,status TEXT)")
                db.execute(f"INSERT INTO {table} VALUES('backfill','pending')")
                self.assertFalse(has_pending_adapter_backfill(db, "P0"))
                self.assertTrue(has_pending_adapter_backfill(db, "P1"))
                self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
                db.close()

    def test_cnstock_search_and_detail_are_p1_adapter_backfill(self):
        for table in ("cnstock_search_jobs", "cnstock_detail_jobs"):
            with self.subTest(table=table):
                db = sqlite3.connect(":memory:")
                db.execute(f"CREATE TABLE {table}(mode TEXT,status TEXT)")
                db.execute(f"INSERT INTO {table} VALUES('backfill','pending')")
                self.assertFalse(has_pending_adapter_backfill(db, "P0"))
                self.assertTrue(has_pending_adapter_backfill(db, "P1"))
                self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
                self.assertTrue(has_pending_adapter_backfill(
                    db, "P1", {"shanghai_securities_news"}))
                self.assertFalse(has_pending_adapter_backfill(
                    db, "P1", {"securities_times"}))
                db.close()

    def test_yicai_search_and_detail_are_p1_adapter_backfill(self):
        for table in ("yicai_search_jobs", "yicai_detail_jobs"):
            with self.subTest(table=table):
                db = sqlite3.connect(":memory:")
                db.execute(f"CREATE TABLE {table}(mode TEXT,status TEXT)")
                db.execute(f"INSERT INTO {table} VALUES('backfill','pending')")
                self.assertFalse(has_pending_adapter_backfill(db, "P0"))
                self.assertTrue(has_pending_adapter_backfill(db, "P1"))
                self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
                db.close()

    def test_fierce_search_and_detail_are_p1_adapter_backfill(self):
        for table in ("fierce_search_jobs", "fierce_detail_jobs"):
            with self.subTest(table=table):
                db = sqlite3.connect(":memory:")
                db.execute(f"CREATE TABLE {table}(mode TEXT,status TEXT)")
                db.execute(f"INSERT INTO {table} VALUES('backfill','pending')")
                self.assertFalse(has_pending_adapter_backfill(db, "P0"))
                self.assertTrue(has_pending_adapter_backfill(db, "P1"))
                self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
                db.close()

    def test_biospace_sitemap_and_detail_are_p1_adapter_backfill(self):
        for table in ("biospace_sitemap_jobs", "biospace_detail_jobs"):
            with self.subTest(table=table):
                db = sqlite3.connect(":memory:")
                db.execute(f"CREATE TABLE {table}(mode TEXT,status TEXT)")
                db.execute(f"INSERT INTO {table} VALUES('backfill','pending')")
                self.assertFalse(has_pending_adapter_backfill(db, "P0"))
                self.assertTrue(has_pending_adapter_backfill(db, "P1"))
                self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
                db.close()

    def test_toms_sitemap_and_detail_are_p1_adapter_backfill(self):
        for table in ("toms_sitemap_jobs", "toms_detail_jobs"):
            with self.subTest(table=table):
                db = sqlite3.connect(":memory:")
                db.execute(f"CREATE TABLE {table}(mode TEXT,status TEXT)")
                db.execute(f"INSERT INTO {table} VALUES('backfill','pending')")
                self.assertFalse(has_pending_adapter_backfill(db, "P0"))
                self.assertTrue(has_pending_adapter_backfill(db, "P1"))
                self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
                db.close()

    def test_trendforce_archive_and_detail_are_p1_adapter_backfill(self):
        for table in ("trendforce_archive_jobs", "trendforce_detail_jobs"):
            with self.subTest(table=table):
                db = sqlite3.connect(":memory:")
                db.execute(f"CREATE TABLE {table}(mode TEXT,status TEXT)")
                db.execute(f"INSERT INTO {table} VALUES('backfill','pending')")
                self.assertFalse(has_pending_adapter_backfill(db, "P0"))
                self.assertTrue(has_pending_adapter_backfill(db, "P1"))
                self.assertTrue(has_pending_adapter_backfill(db, "ALL"))
                db.close()


if __name__ == "__main__":
    unittest.main()
