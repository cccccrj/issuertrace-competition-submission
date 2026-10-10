import sqlite3
import unittest

from collector.sse_mirror import HOST_FALLBACKS, carry_metadata, fallback_url, stranded_rows

QUARANTINED = "https://static.sse.com.cn/stock/disclosure/announcement/c/202512/002170_20251230_B8QS.pdf"


def queue(rows, quarantined_hosts=("static.sse.com.cn",)):
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE discovered_metadata(
        source_id TEXT, canonical_url TEXT, title TEXT, published_at TEXT, company_id TEXT,
        matched_legal_entity TEXT, document_type TEXT, original_timezone TEXT, review_status TEXT,
        PRIMARY KEY(source_id, canonical_url))""")
    db.execute("""CREATE TABLE crawl_frontier(
        source_id TEXT, url TEXT, status TEXT, last_error TEXT, PRIMARY KEY(source_id, url))""")
    db.execute("""CREATE TABLE host_state(
        source_id TEXT, host TEXT, state TEXT, reason TEXT, PRIMARY KEY(source_id, host))""")
    db.executemany("INSERT INTO crawl_frontier VALUES('sse_ipo_review',?,?,?)", rows)
    for host in quarantined_hosts:
        db.execute("INSERT INTO host_state VALUES('sse_ipo_review',?,'manual_review','challenge')", (host,))
    db.commit()
    return db


class FallbackUrlTests(unittest.TestCase):
    def test_the_path_is_kept_and_only_the_host_changes(self):
        self.assertEqual(
            fallback_url(QUARANTINED),
            "https://www.sse.com.cn/stock/disclosure/announcement/c/202512/002170_20251230_B8QS.pdf")

    def test_a_host_with_no_declared_counterpart_gets_none(self):
        # The map is two named hosts of one publisher, not a way to retry
        # anything anywhere.
        self.assertIsNone(fallback_url("https://static.cninfo.com.cn/finalpage/2026-01-01/1.PDF"))
        self.assertIsNone(fallback_url("https://www.sse.com.cn/already/there.pdf"))

    def test_the_map_only_points_away_from_quarantined_hosts(self):
        for source, target in HOST_FALLBACKS.items():
            self.assertNotEqual(source, target)


class StrandedRowTests(unittest.TestCase):
    def test_rows_parked_by_the_quarantined_host_are_picked_up(self):
        db = queue([(QUARANTINED, "manual_review", "challenge")])
        self.assertEqual([url for url, _ in stranded_rows(db, "sse_ipo_review")], [QUARANTINED])
        db.close()

    def test_a_row_parked_for_another_reason_on_another_host_is_left_alone(self):
        db = queue([("https://other.example.com/a.pdf", "manual_review", "oversize")])
        self.assertEqual(stranded_rows(db, "sse_ipo_review"), [])
        db.close()

    def test_nothing_is_retried_once_the_host_is_no_longer_quarantined(self):
        # If the quarantine were lifted the normal collector handles it; this
        # route exists only while the host is actually blocked.
        db = queue([(QUARANTINED, "manual_review", "challenge")], quarantined_hosts=())
        self.assertEqual(stranded_rows(db, "sse_ipo_review"), [])
        db.close()

    def test_a_row_already_obtained_is_not_offered(self):
        db = queue([(QUARANTINED, "superseded", "obtained from www.sse.com.cn")])
        self.assertEqual(stranded_rows(db, "sse_ipo_review"), [])
        db.close()


class MetadataCarryTests(unittest.TestCase):
    def test_the_type_and_company_follow_the_document_to_the_other_host(self):
        # Without this the file arrives typed "pdf" with no company, and both the
        # evidence index and the version comparison select by type -- collected,
        # parsed, and impossible to cite.
        db = queue([(QUARANTINED, "manual_review", "challenge")])
        db.execute("""INSERT INTO discovered_metadata VALUES('sse_ipo_review',?,?,?,?,?,?,?,?)""",
                   (QUARANTINED, "招股说明书（申报稿）", "2025-12-30", "cxmt", "长鑫科技集团股份有限公司",
                    "prospectus", "Asia/Shanghai", None))
        db.commit()
        target = fallback_url(QUARANTINED)
        self.assertTrue(carry_metadata(db, "sse_ipo_review", QUARANTINED, target))
        row = db.execute("SELECT company_id,document_type,published_at FROM discovered_metadata WHERE canonical_url=?",
                         (target,)).fetchone()
        self.assertEqual(row, ("cxmt", "prospectus", "2025-12-30"))
        db.close()

    def test_a_document_with_no_hint_is_reported_not_invented(self):
        db = queue([(QUARANTINED, "manual_review", "challenge")])
        self.assertFalse(carry_metadata(db, "sse_ipo_review", QUARANTINED, fallback_url(QUARANTINED)))
        db.close()


if __name__ == "__main__":
    unittest.main()
