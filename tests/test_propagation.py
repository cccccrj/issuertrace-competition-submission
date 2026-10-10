import unittest

from processor.propagation import (
    as_moment, components, copy_kind, depths, lag_hours, reaction_kind, sequence_edges, similarity,
)
from processor.pipeline import tokenize


def document(version_id, title, summary, published_at, tier="S2", issues=("EXPORT_CTRL",),
             company="wuxi_apptec"):
    return {"version_id": version_id, "company_id": company, "title": title, "summary": summary,
            "published_at": published_at, "source_tier": tier, "issue_codes": list(issues),
            "tokens": tokenize(f"{title}\n{summary}")}


class MomentTests(unittest.TestCase):
    def test_a_bare_date_and_a_full_timestamp_are_both_understood(self):
        self.assertIsNotNone(as_moment("2026-05-20"))
        self.assertIsNotNone(as_moment("2026-08-24T02:28:41.759Z"))

    def test_an_unparseable_value_is_not_guessed_at(self):
        self.assertIsNone(as_moment("上周"))
        self.assertIsNone(as_moment(None))

    def test_lag_is_measured_in_hours_between_the_two_publications(self):
        self.assertEqual(lag_hours("2026-05-20", "2026-05-21"), 24.0)

    def test_lag_is_negative_when_the_later_document_came_first(self):
        self.assertLess(lag_hours("2026-05-21", "2026-05-20"), 0)

    def test_lag_is_unknown_rather_than_zero_when_a_date_is_missing(self):
        self.assertIsNone(lag_hours(None, "2026-05-20"))


class CopyKindTests(unittest.TestCase):
    def test_one_source_repeating_its_own_template_filing_is_not_a_repost(self):
        # A company filing the same daily return every day did not have its story
        # picked up by anyone.
        self.assertEqual(copy_kind(0.99, same_source=True), "RECURRING")

    def test_another_outlet_carrying_the_same_text_is_a_repost(self):
        self.assertEqual(copy_kind(0.95, same_source=False), "REPOST")

    def test_a_looser_match_from_another_outlet_is_a_rewrite(self):
        self.assertEqual(copy_kind(0.75, same_source=False), "REWRITE")


class ReactionKindTests(unittest.TestCase):
    def setUp(self):
        self.report = document("m1", "药明康德被列入草案清单", "媒体报道公司被列入生物安全法草案清单", "2026-01-01")

    def test_a_denial_is_a_clarification(self):
        later = document("m2", "药明康德澄清：没有人类基因组学业务", "公司称相关传闻不实", "2026-01-02")
        self.assertEqual(reaction_kind(later, self.report, 0.4), "CLARIFICATION")

    def test_a_correction_outranks_a_clarification_when_both_words_appear(self):
        later = document("m3", "更正公告：此前澄清内容有误", "公司更正前次公告", "2026-01-03")
        self.assertEqual(reaction_kind(later, self.report, 0.4), "CORRECTION")

    def test_an_official_reply_that_says_so_is_a_response(self):
        later = document("a1", "关于媒体报道的回应公告", "公司就相关报道作出说明", "2026-01-04", tier="S1")
        self.assertEqual(reaction_kind(later, self.report, 0.4), "RESPONSE")

    def test_a_routine_results_release_after_a_report_is_not_a_response_to_it(self):
        # Without this the graph fills with conversations that never took place.
        later = document("a2", "药明康德2026年第一季度业绩公告", "公司公布季度收入与利润", "2026-01-05", tier="S1")
        self.assertEqual(reaction_kind(later, self.report, 0.4), "FOLLOW_UP")

    def test_a_media_report_repeating_the_topic_is_a_follow_up(self):
        later = document("m4", "草案清单事件持续发酵", "多家机构关注生物安全法草案清单进展", "2026-01-06")
        self.assertEqual(reaction_kind(later, self.report, 0.35), "FOLLOW_UP")

    def test_an_unrelated_later_document_produces_no_edge(self):
        later = document("m5", "公司完成董事换届", "董事会完成换届选举", "2026-01-07")
        self.assertIsNone(reaction_kind(later, self.report, 0.05))

    def test_denial_wording_needs_only_slight_overlap_to_be_recognised(self):
        later = document("m6", "公司辟谣", "相关传闻严重失实", "2026-01-02")
        self.assertEqual(reaction_kind(later, self.report, 0.2), "CLARIFICATION")


class SequenceEdgeTests(unittest.TestCase):
    def _rows(self):
        return [document("m1", "公司被列入草案清单", "媒体报道公司被列入生物安全法草案清单", "2026-01-01"),
                document("m2", "公司被列入草案清单进展", "媒体报道公司被列入生物安全法草案清单最新进展", "2026-01-02"),
                document("m3", "公司被列入草案清单再追踪", "媒体报道公司被列入生物安全法草案清单后续追踪", "2026-01-03")]

    def test_each_event_links_to_one_antecedent_not_to_every_earlier_one(self):
        # Three related reports give two edges, not the three a full mesh would.
        edges = sequence_edges(self._rows(), set())
        self.assertEqual(len(edges), 2)
        self.assertEqual(sorted(edge["to"] for edge in edges), ["m2", "m3"])
        self.assertTrue(all(edge["from"] < edge["to"] for edge in edges))

    def test_a_pair_already_linked_as_a_copy_is_not_linked_a_second_time(self):
        edges = sequence_edges(self._rows(), {("m1", "m2"), ("m2", "m1")})
        self.assertNotIn(("m1", "m2"), {(edge["from"], edge["to"]) for edge in edges})

    def test_an_antecedent_outside_the_lookback_window_is_not_linked(self):
        rows = self._rows()
        rows[2]["published_at"] = "2026-06-01"
        edges = sequence_edges(rows, set())
        self.assertNotIn("m3", {edge["to"] for edge in edges})

    def test_edges_stay_inside_one_issue(self):
        rows = self._rows()
        rows[1]["issue_codes"] = ["FINANCIAL"]
        self.assertTrue(all(edge["issue_code"] in {"EXPORT_CTRL", "FINANCIAL"}
                            for edge in sequence_edges(rows, set())))


class ComponentTests(unittest.TestCase):
    def _edge(self, kind, source, target):
        return {"kind": kind, "from": source, "to": target, "company_id": "cxmt"}

    def test_a_reprint_chain_collapses_into_one_story(self):
        versions = {"a": "cxmt", "b": "cxmt", "c": "cxmt"}
        groups, _ = components([self._edge("REPOST", "a", "b"), self._edge("REPOST", "a", "c")], versions)
        self.assertEqual([sorted(group) for group in groups], [["a", "b", "c"]])

    def test_a_report_nobody_copied_is_its_own_independent_story(self):
        groups, _ = components([], {"a": "cxmt", "b": "cxmt"})
        self.assertEqual(len(groups), 2)

    def test_quoting_the_same_prospectus_does_not_make_two_reports_one_story(self):
        # 源于披露 links an article to a filing; if it merged components, every
        # article citing the prospectus would count as a single voice.
        groups, _ = components([self._edge("FROM_DISCLOSURE", "filing", "a"),
                                self._edge("FROM_DISCLOSURE", "filing", "b")],
                               {"a": "cxmt", "b": "cxmt", "filing": "cxmt"})
        self.assertEqual(len(groups), 3)

    def test_same_source_recurrence_still_collapses_because_it_is_one_voice(self):
        groups, _ = components([self._edge("RECURRING", "a", "b")], {"a": "cxmt", "b": "cxmt"})
        self.assertEqual(len(groups), 1)

    def test_depth_counts_hops_from_the_originating_document(self):
        _, adjacency = components([self._edge("REPOST", "a", "b"), self._edge("REPOST", "b", "c")],
                                  {"a": "cxmt", "b": "cxmt", "c": "cxmt"})
        self.assertEqual(depths("a", ["a", "b", "c"], adjacency), {"a": 0, "b": 1, "c": 2})


class SimilarityTests(unittest.TestCase):
    def test_two_empty_token_sets_are_not_treated_as_identical(self):
        self.assertEqual(similarity(set(), set()), 0.0)

    def test_identical_token_sets_score_one(self):
        self.assertEqual(similarity({"甲", "乙"}, {"甲", "乙"}), 1.0)


if __name__ == "__main__":
    unittest.main()
