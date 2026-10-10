"""I-04: a deadline or target still in the future when reported is not when anything happened."""
import sqlite3
import unittest

from processor.pipeline import event_time_role
from processor.timeline import timeline_time


class RoleTests(unittest.TestCase):
    def test_a_far_future_deadline_is_scheduled(self):
        # BIOSECURE: contracts to end by 2032-01-01, reported 2024-05-10.
        self.assertEqual(event_time_role("2032-01-01", "2024-05-10T08:00:00Z"), "scheduled")

    def test_a_future_production_year_is_scheduled(self):
        self.assertEqual(event_time_role("2027-01-01/2027-12-31", "2026-02-04"), "scheduled")

    def test_a_meeting_announced_for_next_month_is_scheduled(self):
        self.assertEqual(event_time_role("2026-09-29", "2026-08-10"), "scheduled")

    def test_the_publication_day_has_happened(self):
        self.assertEqual(event_time_role("2026-08-10", "2026-08-10T02:00:00Z"), "occurred")

    def test_one_day_of_slack_absorbs_utc_storage(self):
        # Written on the evening of the 10th in Beijing, stored as the 9th in UTC.
        self.assertEqual(event_time_role("2026-08-10", "2026-08-09T15:00:00Z"), "occurred")

    def test_a_range_spanning_publication_has_occurred(self):
        self.assertEqual(event_time_role("2026-01-01/2026-12-31", "2026-02-04"), "occurred")

    def test_the_past_has_occurred(self):
        self.assertEqual(event_time_role("2023-03-01", "2026-02-04"), "occurred")

    def test_no_time_no_role(self):
        self.assertIsNone(event_time_role(None, "2026-02-04"))
        self.assertIsNone(event_time_role("截至11:00", "2026-02-04"))

    def test_no_publication_date_is_not_called_a_plan(self):
        self.assertEqual(event_time_role("2032-01-01", None), "occurred")


class TimelineTests(unittest.TestCase):
    def test_a_scheduled_event_sits_at_publication(self):
        at, basis, _ = timeline_time({"event_time": "2032-01-01", "event_time_role": "scheduled",
                                      "published_at": "2024-05-10"})
        self.assertEqual((at, basis), ("2024-05-10", "published_at"))

    def test_an_occurred_event_keeps_its_own_time(self):
        at, basis, _ = timeline_time({"event_time": "2023-03-01", "event_time_role": "occurred",
                                      "published_at": "2024-05-10"})
        self.assertEqual((at, basis), ("2023-03-01", "event_time"))


class LedgerTests(unittest.TestCase):
    def test_scheduled_dates_stay_out_of_the_occurrence_range(self):
        from processor.pipeline import ensure_schema, rebuild_issue_ledger
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, published_at TEXT,
                      source_id TEXT, source_tier TEXT)""")
        ensure_schema(db)
        for event_id, when, role, published in (("a", "2023-03-01", "occurred", "2024-01-01"),
                                                ("b", "2032-01-01", "scheduled", "2024-05-10")):
            db.execute("INSERT INTO document_versions VALUES(?,?,?,?)", (event_id, published, "s", "S2"))
            db.execute("""INSERT INTO event_records(event_id,version_id,run_id,company_id,event_types_json,
                issue_codes_json,production_mode,event_subjects_json,affected_objects_json,impact_direction,
                impact_magnitude,article_sentiment,relevance_score,eventhood,summary,amounts_json,ratios_json,
                persons_json,organizations_json,products_json,risk_score,risk_level,recommended_action,
                label_source,extraction_confidence,needs_review,raw_label_json,created_at,updated_at,
                event_time,event_time_role)
                VALUES(?,?,'r','co','[]','["FINANCIAL"]','BRIEF','[]','[]','neutral','minor','neutral',0.5,
                'EVENT','s','[]','[]','[]','[]','[]',10,'low','watch','ai',0.9,0,'{}','t','t',?,?)""",
                       (event_id, event_id, when, role))
        rebuild_issue_ledger(db)
        earliest, latest = db.execute(
            "SELECT event_time_earliest, event_time_latest FROM issue_ledger").fetchone()
        self.assertEqual((earliest, latest), ("2023-03-01", "2023-03-01"))


if __name__ == "__main__":
    unittest.main()
