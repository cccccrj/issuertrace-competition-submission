import sqlite3
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from collector.federal_register_api import ensure_jobs as ensure_federal_jobs
from collector.sse_api import ensure_jobs as ensure_sse_jobs


class IncrementalJobTests(unittest.TestCase):
    def test_sse_creates_daily_jobs_and_requeues_current_day_each_hour(self):
        db = sqlite3.connect(":memory:")
        config = {"scope": {"backfill_start": "2024-01-01", "snapshot_as_of": "2024-01-31",
                            "incremental_timezone": "Asia/Shanghai"}}
        now = datetime(2024, 2, 2, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        ensure_sse_jobs(db, config, now)
        self.assertEqual(db.execute("SELECT count(*) FROM api_jobs WHERE job_id LIKE 'incremental:%'").fetchone()[0], 4)
        db.execute("""UPDATE api_jobs SET status='done',processed_at='2024-02-02T01:00:00+00:00'
            WHERE job_id LIKE 'incremental:%:2024-02-02:%'""")
        db.commit()
        ensure_sse_jobs(db, config, now)
        self.assertEqual(db.execute("""SELECT count(*) FROM api_jobs
            WHERE job_id LIKE 'incremental:%:2024-02-02:%' AND status='pending'""").fetchone()[0], 2)
        db.close()

    def test_federal_register_creates_daily_jobs_after_snapshot(self):
        db = sqlite3.connect(":memory:")
        config = {"scope": {"backfill_start": "2024-01-01", "snapshot_as_of": "2024-01-31"}}
        ensure_federal_jobs(db, config, date(2024, 2, 2))
        # Three configured terms across two post-snapshot dates.
        self.assertEqual(db.execute("""SELECT count(*) FROM federal_register_jobs
            WHERE job_id LIKE 'incremental:%'""").fetchone()[0], 6)
        self.assertEqual(db.execute("""SELECT min(start_date),max(end_date)
            FROM federal_register_jobs WHERE job_id LIKE 'incremental:%'""").fetchone(),
                         ("2024-02-01", "2024-02-02"))
        db.close()


if __name__ == "__main__":
    unittest.main()
