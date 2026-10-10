import json
import sqlite3
import unittest
from unittest import mock
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collector.sse_api import ensure_jobs as ensure_sse_jobs
from collector.cninfo_api import (MAX_COOLDOWN_WAIT_SECONDS, SECURITIES, align_with_sse,
                                  announcement_date, clean_title, cooldown_wait, ensure_jobs,
                                  last_refusal, next_pending_document, normalized_title,
                                  page_count_for, pdf_url, query_params)

CONFIG = {"scope": {"backfill_start": "2024-01-01", "snapshot_as_of": "2024-01-31",
                    "incremental_timezone": "Asia/Shanghai"}}


def frontier_db():
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE discovered_metadata(
        source_id TEXT, canonical_url TEXT, title TEXT, published_at TEXT, company_id TEXT,
        PRIMARY KEY(source_id, canonical_url))""")
    db.execute("""CREATE TABLE crawl_frontier(
        source_id TEXT, url TEXT, status TEXT, processed_at TEXT, last_error TEXT,
        PRIMARY KEY(source_id, url))""")
    return db


def strand(db, url, title, published_at="2026-04-02", company_id="wuxi_apptec"):
    db.execute("INSERT INTO discovered_metadata VALUES('sse_announcements',?,?,?,?)",
               (url, title, published_at, company_id))
    db.execute("INSERT INTO crawl_frontier VALUES('sse_announcements',?,'manual_review',NULL,NULL)", (url,))
    db.commit()


class JobPlanTests(unittest.TestCase):
    def test_monthly_backfill_and_daily_increments_for_both_companies(self):
        db = sqlite3.connect(":memory:")
        now = datetime(2024, 2, 2, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        ensure_jobs(db, CONFIG, now)
        self.assertEqual(db.execute("SELECT count(*) FROM api_jobs WHERE job_id NOT LIKE 'incremental:%'"
                                    ).fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT count(*) FROM api_jobs WHERE job_id LIKE 'incremental:%'"
                                    ).fetchone()[0], 4)
        db.close()

    def test_the_current_day_is_requeued_once_an_hour(self):
        db = sqlite3.connect(":memory:")
        now = datetime(2024, 2, 2, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        ensure_jobs(db, CONFIG, now)
        db.execute("""UPDATE api_jobs SET status='done',processed_at='2024-02-02T01:00:00+00:00'
            WHERE job_id LIKE 'incremental:%:2024-02-02:%'""")
        db.commit()
        ensure_jobs(db, CONFIG, now)
        self.assertEqual(db.execute("""SELECT count(*) FROM api_jobs
            WHERE job_id LIKE 'incremental:%:2024-02-02:%' AND status='pending'""").fetchone()[0], 2)
        db.close()

    def test_job_ids_do_not_collide_with_the_exchange_adapter(self):
        # api_jobs.job_id is one key space for every adapter.  Unprefixed ids
        # collide with sse_announcements month for month, INSERT OR IGNORE drops
        # them, and the backfill plans nothing while reporting success.
        db = sqlite3.connect(":memory:")
        now = datetime(2024, 2, 2, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        ensure_sse_jobs(db, CONFIG, now)
        before = db.execute("SELECT count(*) FROM api_jobs").fetchone()[0]
        ensure_jobs(db, CONFIG, now)
        after = db.execute("SELECT count(*) FROM api_jobs").fetchone()[0]
        self.assertEqual(after - before, before)
        self.assertEqual(db.execute("SELECT count(*) FROM api_jobs WHERE source_id=?",
                                    ("cninfo_announcements",)).fetchone()[0], before)
        db.close()

    def test_the_query_carries_the_organisation_id_the_interface_requires(self):
        # A ticker on its own returns an empty page rather than an error, which
        # would look like "this month had no filings".
        params = query_params("wuxi_apptec", "2024-01-01", "2024-01-31", 2)
        self.assertEqual(params["stock"], "603259," + SECURITIES["wuxi_apptec"]["org_id"])
        self.assertEqual(params["seDate"], "2024-01-01~2024-01-31")
        self.assertEqual(params["pageNum"], 2)


class PagingTests(unittest.TestCase):
    def test_the_record_count_decides_not_the_reported_page_count(self):
        # Observed verbatim: 38 records, hasMore true, totalpages 1.  Believing
        # totalpages truncated every busy month to its first 30 filings.
        self.assertEqual(page_count_for(
            {"totalRecordNum": 38, "hasMore": True, "totalpages": 1}), 2)

    def test_an_exact_multiple_does_not_ask_for_an_empty_page(self):
        self.assertEqual(page_count_for({"totalRecordNum": 60, "totalpages": 1}), 2)

    def test_a_single_page_stays_single(self):
        self.assertEqual(page_count_for({"totalRecordNum": 7, "totalpages": 1}), 1)

    def test_no_count_falls_back_to_has_more(self):
        self.assertEqual(page_count_for({"hasMore": True, "announcements": [{}]}), 2)
        self.assertEqual(page_count_for({"hasMore": False, "announcements": [{}]}), 1)

    def test_an_empty_answer_is_one_page(self):
        self.assertEqual(page_count_for({"totalRecordNum": 0, "announcements": []}), 1)


class ItemParsingTests(unittest.TestCase):
    def test_publication_date_is_read_in_the_exchange_timezone(self):
        # 2026-04-02 07:00 UTC is still 2026-04-02 in Shanghai; 17:00 UTC is not.
        self.assertEqual(announcement_date({"announcementTime": 1775113200000}), "2026-04-02")
        self.assertEqual(announcement_date({"announcementTime": 1775149200000}), "2026-04-03")

    def test_a_missing_timestamp_yields_no_date_rather_than_today(self):
        self.assertEqual(announcement_date({}), "")

    def test_search_highlighting_is_stripped_from_titles(self):
        self.assertEqual(clean_title("关于<em>回购</em>股份的公告"), "关于回购股份的公告")

    def test_attachment_paths_resolve_against_the_file_host(self):
        self.assertEqual(pdf_url({"adjunctUrl": "finalpage/2026-08-25/1225500481.PDF"}),
                         "https://static.cninfo.com.cn/finalpage/2026-08-25/1225500481.PDF")

    def test_an_item_without_an_attachment_is_skipped_not_guessed(self):
        self.assertIsNone(pdf_url({"adjunctUrl": ""}))


class DownloadQueueTests(unittest.TestCase):
    def queue(self, rows):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE crawl_frontier(
            source_id TEXT, url TEXT, status TEXT, discovered_at TEXT,
            PRIMARY KEY(source_id, url))""")
        db.executemany("INSERT INTO crawl_frontier VALUES('cninfo_announcements',?,?,?)", rows)
        db.commit()
        return db

    def test_the_oldest_undelivered_document_comes_first(self):
        db = self.queue([("https://x/b.PDF", "pending", "2026-01-02"),
                         ("https://x/a.PDF", "pending", "2026-01-01")])
        self.assertEqual(next_pending_document(db, set()), "https://x/a.PDF")
        db.close()

    def test_a_document_that_kept_failing_is_stepped_over(self):
        # Without this the drain retries one bad URL forever and never reaches
        # the rest of the queue.
        db = self.queue([("https://x/a.PDF", "pending", "2026-01-01"),
                         ("https://x/b.PDF", "pending", "2026-01-02")])
        self.assertEqual(next_pending_document(db, {"https://x/a.PDF"}), "https://x/b.PDF")
        db.close()

    def test_a_retrieved_document_is_not_offered_again(self):
        db = self.queue([("https://x/a.PDF", "done", "2026-01-01")])
        self.assertIsNone(next_pending_document(db, set()))
        db.close()


class CooldownTests(unittest.TestCase):
    def state(self, state, cooldown_until):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE source_state(source_id TEXT PRIMARY KEY, state TEXT, cooldown_until TEXT)")
        db.execute("INSERT INTO source_state VALUES('cninfo_announcements',?,?)", (state, cooldown_until))
        db.commit()
        return db

    def later(self, seconds):
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def test_a_short_cooldown_is_waited_out_not_abandoned(self):
        # One transient 5xx in a 500-document backfill used to end the whole run.
        db = self.state("cooldown", self.later(30))
        waiting = cooldown_wait(db)
        self.assertTrue(0 < waiting <= 31)
        db.close()

    def test_an_expired_cooldown_does_not_wait(self):
        db = self.state("cooldown", self.later(-5))
        self.assertEqual(cooldown_wait(db), 0.0)
        db.close()

    def test_a_cooldown_longer_than_the_cap_stops_the_run(self):
        db = self.state("cooldown", self.later(MAX_COOLDOWN_WAIT_SECONDS + 60))
        self.assertIsNone(cooldown_wait(db))
        db.close()

    def test_a_cooldown_is_a_pause_for_both_loops(self):
        # Discovery and retrieval both used to read "nothing done" as "queue
        # empty"; one 5xx ended an 88-job sweep at job 48, which looks like the
        # venue simply having fewer filings.
        db = self.state("cooldown", self.later(30))
        self.assertTrue(cooldown_wait(db))
        db.close()

    def test_a_quarantined_source_is_never_waited_on(self):
        # manual_review is a decision, not a delay; sleeping past it would be
        # exactly the circumvention the project forbids.
        db = self.state("manual_review", self.later(10))
        self.assertIsNone(cooldown_wait(db))
        db.close()


class RefusalReasonTests(unittest.TestCase):
    def audit_log(self, tmpdir, events):
        path = pathlib.Path(tmpdir) / "collection.jsonl"
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
        return path

    def test_the_collectors_own_reason_is_recovered(self):
        # "response was not saved" invites a retry that cannot succeed; the size
        # refusal is the fact a person needs.
        with tempfile.TemporaryDirectory() as tmp:
            path = self.audit_log(tmp, [
                {"url": "https://x/a.PDF", "outcome": "robots_cached_absent"},
                {"url": "https://x/a.PDF", "outcome": "refused_unsafe_request",
                 "error": "response declares 53555300 bytes; cap is 26214400"},
            ])
            with mock.patch("collector.cninfo_api.AUDIT_LOG", path):
                self.assertEqual(last_refusal("https://x/a.PDF"),
                                 "refused_unsafe_request: response declares 53555300 bytes; cap is 26214400")

    def test_our_own_error_line_is_not_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.audit_log(tmp, [
                {"url": "https://x/a.PDF", "outcome": "refused_unsafe_request", "error": "too big"},
                {"url": "https://x/a.PDF", "outcome": "cninfo_document_error", "error": "response was not saved"},
            ])
            with mock.patch("collector.cninfo_api.AUDIT_LOG", path):
                self.assertEqual(last_refusal("https://x/a.PDF"), "refused_unsafe_request: too big")

    def test_a_missing_log_is_not_a_crash(self):
        with mock.patch("collector.cninfo_api.AUDIT_LOG", pathlib.Path("/nonexistent/collection.jsonl")):
            self.assertIsNone(last_refusal("https://x/a.PDF"))


class AlignmentTests(unittest.TestCase):
    def test_a_matched_filing_retires_the_stranded_exchange_url(self):
        db = frontier_db()
        strand(db, "https://static.sse.com.cn/a.pdf", "关于回购股份的公告")
        matched = align_with_sse(db, "wuxi_apptec", "2026-04-02", "关于回购股份的公告",
                                 "https://static.cninfo.com.cn/finalpage/x.PDF")
        self.assertEqual(matched, "https://static.sse.com.cn/a.pdf")
        status, note = db.execute("SELECT status,last_error FROM crawl_frontier").fetchone()
        self.assertEqual(status, "superseded")
        self.assertIn("https://static.cninfo.com.cn/finalpage/x.PDF", note)
        db.close()

    def test_punctuation_differences_between_venues_still_match(self):
        db = frontier_db()
        strand(db, "https://static.sse.com.cn/a.pdf", "关于回购股份的公告（更正）")
        self.assertIsNotNone(align_with_sse(db, "wuxi_apptec", "2026-04-02", "关于回购股份的公告(更正)",
                                            "https://static.cninfo.com.cn/finalpage/x.PDF"))
        db.close()

    def test_a_different_filing_on_the_same_day_is_not_retired(self):
        # Several announcements share a date; matching on date alone would
        # retire a document that was never obtained.
        db = frontier_db()
        strand(db, "https://static.sse.com.cn/a.pdf", "关于回购股份的公告")
        self.assertIsNone(align_with_sse(db, "wuxi_apptec", "2026-04-02", "2025年年度报告",
                                         "https://static.cninfo.com.cn/finalpage/x.PDF"))
        self.assertEqual(db.execute("SELECT status FROM crawl_frontier").fetchone()[0], "manual_review")
        db.close()

    def test_another_company_is_not_retired_by_a_same_titled_filing(self):
        db = frontier_db()
        strand(db, "https://static.sse.com.cn/a.pdf", "2025年年度报告", company_id="cxmt")
        self.assertIsNone(align_with_sse(db, "wuxi_apptec", "2026-04-02", "2025年年度报告",
                                         "https://static.cninfo.com.cn/finalpage/x.PDF"))
        db.close()

    def test_a_row_already_fetched_from_the_exchange_is_left_alone(self):
        db = frontier_db()
        strand(db, "https://static.sse.com.cn/a.pdf", "关于回购股份的公告")
        db.execute("UPDATE crawl_frontier SET status='done'")
        db.commit()
        self.assertIsNone(align_with_sse(db, "wuxi_apptec", "2026-04-02", "关于回购股份的公告",
                                         "https://static.cninfo.com.cn/finalpage/x.PDF"))
        self.assertEqual(db.execute("SELECT status FROM crawl_frontier").fetchone()[0], "done")
        db.close()

    def test_two_same_titled_filings_on_one_day_retire_two_rows(self):
        # "H股公告" is filed repeatedly under the same title.  The pass takes the
        # first row still awaiting retrieval, so the second document retires the
        # second row instead of re-matching the first and stranding it.
        db = frontier_db()
        strand(db, "https://static.sse.com.cn/a.pdf", "H股公告")
        strand(db, "https://static.sse.com.cn/b.pdf", "H股公告")
        first = align_with_sse(db, "wuxi_apptec", "2026-04-02", "H股公告", "https://x/1.PDF")
        second = align_with_sse(db, "wuxi_apptec", "2026-04-02", "H股公告", "https://x/2.PDF")
        self.assertNotEqual(first, second)
        self.assertEqual(db.execute("SELECT count(*) FROM crawl_frontier WHERE status='superseded'"
                                    ).fetchone()[0], 2)
        db.close()

    def test_titles_differing_by_a_word_do_not_normalise_together(self):
        self.assertNotEqual(normalized_title("2025年年度报告"), normalized_title("2025年半年度报告"))


if __name__ == "__main__":
    unittest.main()
