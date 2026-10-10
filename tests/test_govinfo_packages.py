import sqlite3
import unittest
from urllib.parse import urlparse

from collector.govinfo_packages import PACKAGES, ensure_schema, suspend_pending_jobs


class GovInfoPackageTests(unittest.TestCase):
    def test_verified_packages_are_unique_in_scope_and_same_origin(self):
        self.assertEqual(len(PACKAGES), 9)
        urls = [item["url"] for item in PACKAGES.values()]
        self.assertEqual(len(urls), len(set(urls)))
        for item in PACKAGES.values():
            self.assertEqual(urlparse(item["url"]).hostname, "www.govinfo.gov")
            self.assertTrue(item["url"].endswith(".pdf"))
            self.assertGreaterEqual(item["published_at"], "2024-01-01")

    def test_version_types_do_not_claim_enactment(self):
        types = {item["document_type"] for item in PACKAGES.values()}
        self.assertIn("bill_introduced_house", types)
        self.assertIn("bill_engrossed_house", types)
        self.assertIn("bill_referred_senate", types)
        self.assertIn("bill_reported_senate", types)
        self.assertNotIn("public_law", types)

    def test_schema_seeds_backfill_jobs_and_suspends_pending(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        self.assertEqual(db.execute(
            "SELECT mode,status,count(*) FROM govinfo_package_jobs GROUP BY mode,status"
        ).fetchone(), ("backfill", "pending", 9))
        self.assertEqual(suspend_pending_jobs(db, "robots disallowed"), 9)
        self.assertEqual(db.execute(
            "SELECT count(*) FROM govinfo_package_jobs WHERE status='manual_review'"
        ).fetchone()[0], 9)
        db.close()


if __name__ == "__main__":
    unittest.main()
