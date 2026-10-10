"""风险事项的标题由平台自己命名，不照搬某一家媒体的标题。

改之前 ``processor.alerts.headline`` 就是 ``event["title"]``：277 条预警里 265 条与
首发报道逐字相同，列表里于是出现了「China」「VOLUNTARY ANNOUNCEMENT」这种条目。
那一句回答的是「这篇稿子讲什么」，而风险事项的标题要回答「这条为什么会进到我的
待办里」——两个问题的答案不该共用一句话。

这里逐条钉住那六级规则。顺序即政策，所以测的是「命中哪一条」而不只是「输出非空」：
冲突必须排在传闻之前，否则一条既冲突又低可信的事项会被说成「待核实」，而它其实
已经核出来了。
"""
from __future__ import annotations

import unittest

from api.labels import ISSUE_CODE_LABELS
from processor.alerts import GIST_WIDTH, UNFILED_ISSUE, display_width, event_gist, headline
from processor.pipeline import company_display_name


def title(**overrides):
    fields = {"company_id": "cxmt", "issue_code": "FUND_RAISE", "reasons": set(),
              "verdicts": {}, "phase_label": "申报", "event_count": 3,
              "adjusted_risk_score": 64.0, "summary": None}
    return headline(**{**fields, **overrides})


class SubjectTests(unittest.TestCase):
    def test_the_subject_is_the_company_and_the_issue_a_reader_recognises(self):
        made = title(issue_code="EXPORT_CTRL")
        self.assertTrue(made.startswith(f"{company_display_name('cxmt')}｜"
                                        f"{ISSUE_CODE_LABELS['EXPORT_CTRL']}："), made)

    def test_an_item_with_no_issue_still_gets_a_subject(self):
        # 少一截标题只会让人以为渲染坏了；没有专题也是一条要处置的风险事项。
        self.assertIn(UNFILED_ISSUE, title(issue_code=None))

    def test_the_headline_never_carries_a_company_identifier_from_code(self):
        # 公司名来自 config/sources.yaml，不来自模块常量——E7 的零改代码迁移声明
        # 就是靠这一点成立的（tests.test_migration 扫描整个 processor/）。
        self.assertNotIn("cxmt", title())

    def test_it_stays_inside_the_stored_width(self):
        self.assertLessEqual(len(title(phase_label="注" * 200)), 120)


class RuleLadderTests(unittest.TestCase):
    """六级规则，每级一例，外加两条顺序断言。"""

    def test_a_conflict_with_disclosure_is_said_first(self):
        self.assertIn("报道与披露口径冲突（3 条核验点）",
                      title(verdicts={"CONFLICT": 3, "PARTIAL": 5}))

    def test_a_conflict_outranks_an_unverified_rumour(self):
        # 既冲突又低可信时说「待核实」是错的：它已经核出来了，而且核出了矛盾。
        made = title(verdicts={"CONFLICT": 1}, reasons={"unverified_major_rumour"})
        self.assertIn("冲突", made)
        self.assertNotIn("待核实", made)

    def test_a_high_impact_rumour_says_nothing_could_corroborate_it(self):
        made = title(reasons={"unverified_major_rumour"})
        self.assertIn("待核实", made)
        # 「独立信源」是这个量的唯一名字，「独立佐证」「独立来源」是它被收敛掉的旧名。
        self.assertIn("独立信源", made)
        self.assertNotIn("独立佐证", made)

    def test_a_new_fact_in_a_sensitive_phase_names_the_phase(self):
        self.assertIn("注册阶段出现新增事实",
                      title(reasons={"sensitive_phase_new_fact"}, phase_label="注册"))

    def test_a_partial_verdict_reads_as_a_deviation_not_a_contradiction(self):
        made = title(verdicts={"PARTIAL": 2})
        self.assertIn("报道与披露存在偏差（2 条核验点）", made)
        self.assertNotIn("冲突", made)

    def test_an_escalating_issue_reports_the_volume(self):
        self.assertIn("专题升温，6 篇报道集中",
                      title(reasons={"issue_escalating"}, event_count=6))

    def test_the_fallback_says_what_is_missing_not_what_the_score_was(self):
        # 风险分在列表里自己就是一列。标题里再写一遍，占掉的是能区分这一条的位置。
        made = title(adjusted_risk_score=61.4, event_count=2)
        self.assertIn("待核验", made)
        self.assertIn("2 篇报道集中", made)
        self.assertNotIn("风险分", made)

    def test_a_single_report_is_not_described_as_concentrated(self):
        # 一篇报道说「1 篇报道集中」，读起来像模板没填完。
        self.assertIn("1 篇报道", title(event_count=1))
        self.assertNotIn("集中", title(event_count=1))
        self.assertNotIn("集中", title(reasons={"issue_escalating"}, event_count=1))


class GistTests(unittest.TestCase):
    """标题里那半句「这一条具体说的是什么事」。

    没有它的时候，277 条标题里 162 条与另一条逐字相同，最狠的一条重复 10 次——
    同一家公司、同一个议题、同样是口径冲突的十条事项，在列表里长得一模一样。
    """

    SUMMARY = "药明康德于2024年5月13日在上海证券交易所回购了432,200股A股，总金额为人民币20,046,852元。"

    def test_two_items_of_the_same_kind_no_longer_read_identically(self):
        # 这条测试就是这次改动的理由本身。
        one = title(verdicts={"CONFLICT": 1}, company_id="wuxi_apptec", summary=self.SUMMARY)
        two = title(verdicts={"CONFLICT": 1}, company_id="wuxi_apptec",
                    summary="药明康德董事会审议通过2024年半年度报告等议案，并同意注销部分股票期权。")
        self.assertNotEqual(one, two)
        self.assertIn("回购", one)
        self.assertIn("审议通过", two)

    def test_the_kind_of_problem_is_still_said(self):
        # 事由是补上去的，不是替换掉的：读者仍然要先知道这是哪一类问题。
        made = title(verdicts={"CONFLICT": 2}, company_id="wuxi_apptec", summary=self.SUMMARY)
        self.assertIn("报道与披露口径冲突（2 条核验点）", made)

    def test_the_company_name_is_not_said_twice(self):
        # 它已经在标题最前面了；再重复一次只是把有用的那二十几个字挤掉几个。
        made = title(company_id="wuxi_apptec", summary=self.SUMMARY)
        self.assertEqual(made.count("药明康德"), 1, made)

    def test_the_date_does_not_eat_the_distinguishing_part(self):
        # 日期在列表里自己有一列。写进标题，占掉的正是区分这一条的那几个字。
        gist = event_gist(self.SUMMARY, "wuxi_apptec")
        self.assertNotIn("2024年5月13日", gist)
        self.assertIn("432,200", gist)

    def test_a_number_is_not_cut_in_half_by_its_own_comma(self):
        # 按 ASCII 逗号断句会把 432,200 截成 432。
        self.assertNotIn("432，", event_gist(self.SUMMARY, "wuxi_apptec"))

    def test_nothing_usable_means_nothing_added(self):
        # 宁可少一截，也不要拿半个词充数。
        for summary in (None, "", "   ", "回购。"):
            made = title(verdicts={"CONFLICT": 1}, summary=summary)
            self.assertTrue(made.endswith("报道与披露口径冲突（1 条核验点）"), made)

    def test_a_long_summary_is_trimmed_and_says_so(self):
        gist = event_gist("公司" + "回购股份" * 40, "wuxi_apptec")
        self.assertLessEqual(display_width(gist), GIST_WIDTH)
        self.assertTrue(gist.endswith("…"), gist)

    def test_an_english_summary_gets_the_same_width_not_the_same_character_count(self):
        # 按字符数一刀切时，英文事由只剩四个单词，两条不同的事项又长得一样了。
        gist = event_gist("U.S. government reportedly weighs adding the company "
                          "and other chipmakers to the export blacklist.", "cxmt")
        self.assertGreater(len(gist.split()), 6, gist)
        self.assertLessEqual(display_width(gist), GIST_WIDTH)

    def test_a_leading_parenthetical_is_not_the_substance(self):
        gist = event_gist("（美国时间）在美国哥伦比亚特区联邦地区法院对美国国防部提起诉讼。",
                          "wuxi_apptec")
        self.assertFalse(gist.startswith("（"), gist)
        self.assertIn("提起诉讼", gist)

    def test_a_parenthetical_hiding_behind_the_subject_is_found_too(self):
        # 括注是在公司名被剥掉之后才露到开头的：各剥一次不够，要剥到不动为止。
        gist = event_gist("药明康德（美国时间）在美国哥伦比亚特区联邦地区法院提起诉讼。",
                          "wuxi_apptec")
        self.assertFalse(gist.startswith("（"), gist)

    def test_the_headline_still_fits_the_stored_width(self):
        self.assertLessEqual(len(title(summary="公司" + "回购股份" * 200,
                                       phase_label="注" * 200)), 120)


if __name__ == "__main__":
    unittest.main()
