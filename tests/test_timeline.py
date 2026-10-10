import unittest

from processor.pipeline import tokenize
from processor.timeline import adjusted_risk, issue_status, novelty, timeline_time


class TimelineTimeTests(unittest.TestCase):
    def test_a_normalized_event_time_wins_over_the_publication_date(self):
        at, basis, precision = timeline_time(
            {"event_time": "2023-04-01", "event_time_precision": "day", "published_at": "2026-08-01"})
        self.assertEqual((at, basis, precision), ("2023-04-01", "event_time", "day"))

    def test_an_event_time_range_is_placed_at_its_start(self):
        at, basis, _ = timeline_time({"event_time": "2026-01-01/2026-03-31", "published_at": "2026-08-01"})
        self.assertEqual((at, basis), ("2026-01-01", "event_time"))

    def test_a_missing_event_time_falls_back_to_publication_and_says_so(self):
        # The position on the axis is then when it was reported, not when it
        # happened, and the reader has to be able to see that.
        at, basis, _ = timeline_time({"event_time": None, "published_at": "2026-08-01"})
        self.assertEqual((at, basis), ("2026-08-01", "published_at"))

    def test_free_text_left_in_event_time_is_not_used_as_a_position(self):
        at, basis, _ = timeline_time({"event_time": "近期", "published_at": "2026-08-01"})
        self.assertEqual((at, basis), ("2026-08-01", "published_at"))

    def test_collection_time_is_the_last_resort(self):
        at, basis, _ = timeline_time({"event_time": None, "published_at": None,
                                      "fetched_at": "2026-08-02T05:00:00Z"})
        self.assertEqual((at, basis), ("2026-08-02T05:00:00Z", "fetched_at"))

    def test_an_event_with_no_usable_date_is_marked_rather_than_dated(self):
        at, basis, precision = timeline_time({"event_time": None, "published_at": None, "fetched_at": None})
        self.assertEqual((at, basis, precision), (None, "none", None))

    def test_publication_precision_reflects_whether_a_time_of_day_was_given(self):
        self.assertEqual(timeline_time({"published_at": "2026-08-01"})[2], "day")
        self.assertEqual(timeline_time({"published_at": "2026-08-01T02:28:41Z"})[2], "second")


class AdjustedRiskTests(unittest.TestCase):
    def test_the_phase_coefficient_multiplies_the_score(self):
        self.assertEqual(adjusted_risk(55.0, 1.3), (71.5, "medium"))

    def test_a_phase_without_a_correction_leaves_the_score_alone(self):
        self.assertEqual(adjusted_risk(55.0, 1.0), (55.0, "medium"))

    def test_the_adjusted_score_cannot_exceed_the_top_of_the_scale(self):
        self.assertEqual(adjusted_risk(90.0, 1.3), (100.0, "high"))

    def test_the_correction_can_move_an_event_into_the_high_band(self):
        self.assertEqual(adjusted_risk(70.0, 1.25)[1], "high")


class NoveltyTests(unittest.TestCase):
    def test_a_rewording_of_something_already_said_is_not_a_new_fact(self):
        earlier = [tokenize("公司本次拟募集资金295亿元用于产能建设")]
        later = [tokenize("公司本次拟募集资金295亿元用于产能建设项目")]
        self.assertEqual(novelty(later, earlier), 0)

    def test_a_claim_the_issue_has_not_heard_counts_as_new(self):
        earlier = [tokenize("公司本次拟募集资金295亿元用于产能建设")]
        later = [tokenize("美国商务部将公司列入实体清单并限制设备出口")]
        self.assertEqual(novelty(later, earlier), 1)

    def test_the_first_event_on_an_issue_is_all_new(self):
        self.assertEqual(novelty([tokenize("公司完成注册")], []), 1)

    def test_an_empty_claim_contributes_nothing(self):
        self.assertEqual(novelty([set()], []), 0)


class IssueStatusTests(unittest.TestCase):
    REFERENCE = "2026-08-26"

    def test_an_issue_with_nothing_recent_is_dormant(self):
        status, *_ = issue_status(["2025-01-01", "2025-02-01"], self.REFERENCE)
        self.assertEqual(status, "dormant")

    def test_an_issue_that_stopped_last_month_is_cooling_not_dormant(self):
        status, *_ = issue_status(["2026-07-01", "2026-07-10"], self.REFERENCE)
        self.assertEqual(status, "cooling")

    def test_an_issue_that_only_just_appeared_is_emerging(self):
        status, *_ = issue_status(["2026-08-20", "2026-08-24"], self.REFERENCE)
        self.assertEqual(status, "emerging")

    def test_more_events_than_the_previous_window_is_escalating(self):
        status, recent, previous, _ = issue_status(
            ["2025-01-01", "2026-07-05", "2026-08-10", "2026-08-20", "2026-08-24"], self.REFERENCE)
        self.assertEqual((status, recent, previous), ("escalating", 3, 1))

    def test_steady_coverage_is_active_rather_than_escalating(self):
        status, *_ = issue_status(
            ["2025-01-01", "2026-07-05", "2026-07-10", "2026-07-28", "2026-08-10"], self.REFERENCE)
        self.assertEqual(status, "active")

    def test_a_planned_future_event_does_not_make_a_dead_issue_look_alive(self):
        # An event time may point at planned 2032 capacity; that says nothing
        # about whether anyone is writing about the issue now.
        status, *_ = issue_status(["2024-01-01", "2032-01-01"], self.REFERENCE)
        self.assertEqual(status, "dormant")

    def test_an_issue_whose_events_have_no_dates_is_reported_as_such(self):
        status, *_ = issue_status([], self.REFERENCE)
        self.assertEqual(status, "empty")

    def test_the_reason_names_the_counts_it_was_decided_from(self):
        *_, reason = issue_status(["2026-08-20", "2026-07-05", "2024-01-01"], self.REFERENCE)
        self.assertIn("30", reason)


if __name__ == "__main__":
    unittest.main()
