import unittest

from processor.disclosure_phase import calendar, phase_at


def filing(version_id, title, published_at):
    return {"version_id": version_id, "title": title, "published_at": published_at}


CXMT_CHAIN = [
    filing("v1", "申报稿_招股说明书", "2025-12-30"),
    filing("v2", "更新申报稿_招股说明书", "2026-05-17"),
    filing("v3", "上市委会议公告", "2026-05-20"),
    filing("v4", "注册稿_招股说明书", "2026-05-27"),
    filing("v5", "证监会注册批复", "2026-06-12"),
    filing("v6", "招股意向书", "2026-07-09"),
    filing("v7", "上市公告书", "2026-07-24"),
]


class CalendarTests(unittest.TestCase):
    def _dates(self, rows):
        return {entry["phase"]: entry["started_at"] for entry in calendar(rows)}

    def test_the_original_filing_starts_the_filing_phase_not_the_updated_one(self):
        # 「更新申报稿」 also contains 「申报稿」; letting it win would move the start
        # of the review phase five months after the company actually filed.
        self.assertEqual(self._dates(CXMT_CHAIN)["FILING"], "2025-12-30")

    def test_every_regulatory_milestone_gets_its_own_phase(self):
        self.assertEqual(self._dates(CXMT_CHAIN),
                         {"PRE_FILING": None, "FILING": "2025-12-30", "MEETING": "2026-05-20",
                          "REGISTRATION": "2026-05-27", "APPROVED": "2026-06-12",
                          "ISSUANCE": "2026-07-09", "LISTED": "2026-07-24"})

    def test_each_phase_ends_where_the_next_one_starts(self):
        phases = calendar(CXMT_CHAIN)
        for earlier, later in zip(phases, phases[1:]):
            self.assertEqual(earlier["ended_at"], later["started_at"])
        self.assertIsNone(phases[-1]["ended_at"])

    def test_a_milestone_that_was_never_collected_is_absent_rather_than_guessed(self):
        chain = [row for row in CXMT_CHAIN if row["title"] != "上市委会议公告"]
        self.assertNotIn("MEETING", self._dates(chain))

    def test_a_company_with_no_filing_chain_has_no_calendar(self):
        # Otherwise every listed company would be reported as 申报前 forever.
        self.assertEqual(calendar([filing("v1", "2025年半年度报告", "2025-08-01")]), [])

    def test_an_out_of_order_anchor_is_dropped_instead_of_breaking_the_order(self):
        chain = CXMT_CHAIN + [filing("v8", "上市公告书", "2020-01-01")]
        phases = calendar(chain)
        starts = [entry["started_at"] for entry in phases if entry["started_at"]]
        self.assertEqual(starts, sorted(starts))

    def test_the_anchor_filing_is_recorded_so_a_boundary_can_be_checked(self):
        meeting = next(entry for entry in calendar(CXMT_CHAIN) if entry["phase"] == "MEETING")
        self.assertEqual((meeting["anchor_version_id"], meeting["anchor_title"]), ("v3", "上市委会议公告"))


class PhaseAtTests(unittest.TestCase):
    def setUp(self):
        self.phases = calendar(CXMT_CHAIN)

    def test_a_date_inside_the_offering_window_gets_the_offering_coefficient(self):
        found = phase_at(self.phases, "2026-07-15")
        self.assertEqual(found["phase"], "ISSUANCE")
        self.assertEqual(found["coefficient"], 1.3)

    def test_a_boundary_date_belongs_to_the_phase_it_opens(self):
        self.assertEqual(phase_at(self.phases, "2026-05-20")["phase"], "MEETING")

    def test_a_date_before_the_first_filing_is_pre_filing(self):
        self.assertEqual(phase_at(self.phases, "2024-03-01")["phase"], "PRE_FILING")

    def test_a_full_timestamp_is_matched_on_its_date(self):
        self.assertEqual(phase_at(self.phases, "2026-07-15T02:28:41.759Z")["phase"], "ISSUANCE")

    def test_a_missing_date_gets_no_correction_rather_than_an_assumed_phase(self):
        found = phase_at(self.phases, None)
        self.assertEqual((found["phase"], found["coefficient"]), ("UNKNOWN", 1.0))

    def test_a_company_without_a_calendar_gets_no_correction(self):
        self.assertEqual(phase_at([], "2026-07-15")["coefficient"], 1.0)


if __name__ == "__main__":
    unittest.main()
