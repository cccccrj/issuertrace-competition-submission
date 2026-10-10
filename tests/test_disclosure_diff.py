import sqlite3
import unittest

from processor.disclosure_diff import (
    align_sections, diff_section, drop_stale_diffs, is_period_rollover, join_gap,
    metric_name, normalize_paragraph,
    numeric_changes, paragraphs, period_anchors, running_lines,
    stage_rank, STAGE_ORDER,
)


def page(number, *lines):
    return "\n".join([f"[PAGE {number}]", *lines])


class ParagraphTests(unittest.TestCase):
    def test_wrapped_lines_are_rejoined_into_one_paragraph(self):
        # PDF extraction breaks a sentence across rendered lines; without
        # rejoining, repagination alone would read as an edit.
        text = page(1, "公司于2026年7月27日在上海证券交易所科创板完成首次公开发行股票并",
                    "上市，本次发行募集资金总额为人民币295亿元。")
        found = paragraphs(text)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["text"].endswith("295亿元。"))

    def test_paragraph_keeps_the_page_it_started_on(self):
        text = page(7, "公司报告期内营业收入持续增长并且毛利率保持稳定水平，具体情况如下")
        self.assertEqual(paragraphs(text)[0]["page"], 7)

    def test_structural_markers_start_a_new_paragraph(self):
        text = page(1, "（一）本次发行的基本情况说明如下所示，具体条款参见后文相关章节的详细描述。",
                    "（二）本次发行的承销方式为余额包销，具体安排参见后文相关章节的详细描述。")
        self.assertEqual(len(paragraphs(text)), 2)

    def test_section_headings_are_tracked(self):
        text = "\n".join([page(3, "第三节 风险因素"),
                          "公司面临市场竞争加剧的风险，可能对未来经营业绩与财务状况产生不利影响。"])
        self.assertEqual(paragraphs(text)[0]["section"], "第三节 风险因素")

    def test_contents_entries_are_ignored(self):
        text = page(2, "第六节 财务会计信息与管理层分析........................253",
                    "公司报告期内的财务状况与经营成果具体分析如下所述，详见本节内容。")
        found = paragraphs(text)
        self.assertEqual(len(found), 1)
        self.assertNotIn("....", found[0]["text"])

    def test_running_footers_are_detected_across_pages(self):
        lines = [(number, f"长鑫科技集团股份有限公司招股说明书1-1-{number}") for number in range(1, 21)]
        lines.append((1, "这是一段真实的披露内容，不应当被识别为页眉页脚。"))
        detected = running_lines(lines)
        self.assertIn("长鑫科技集团股份有限公司招股说明书#-#-#", detected)
        self.assertNotIn("这是一段真实的披露内容，不应当被识别为页眉页脚。", detected)

    def test_running_footer_is_removed_but_varying_prose_is_kept(self):
        # Each page carries the same footer and genuinely different prose, which
        # is what separates chrome from content.
        topics = ["市场竞争", "技术迭代", "客户集中", "供应链保障", "汇率波动", "存货跌价",
                  "产能爬坡", "专利许可", "人才流失", "政策变化", "价格周期", "关联交易",
                  "募投进度", "商誉减值", "环保合规", "海外销售", "税收优惠", "股权结构",
                  "资金成本", "信息安全"]
        body = "\n".join(
            page(number, "长鑫科技集团股份有限公司招股说明书1-1-%d" % number,
                 f"关于{topic}的专项说明，公司已在本节中披露相关影响与应对措施的详细情况。")
            for number, topic in enumerate(topics, start=1))
        found = paragraphs(body)
        self.assertTrue(all("1-1-" not in item["text"] for item in found))
        self.assertEqual(len(found), 20)


class NumericChangeTests(unittest.TestCase):
    def test_figures_pair_up_when_wording_is_identical(self):
        left = "报告期内，公司营业收入为241.78亿元，毛利率为35.20%。"
        right = "报告期内，公司营业收入为617.99亿元，毛利率为35.20%。"
        changes = numeric_changes(left, right)
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["old_value"], changes[0]["new_value"]), ("241.78", "617.99"))
        self.assertIn("营业收入", changes[0]["metric"])

    def test_delta_and_ratio_are_computed(self):
        changes = numeric_changes("持股比例为2.6561%。", "持股比例为2.6456%。")
        self.assertAlmostEqual(changes[0]["delta"], -0.0105, places=4)
        self.assertLess(changes[0]["delta_ratio"], 0)

    def test_rewritten_wording_is_not_treated_as_a_figure_change(self):
        # Pairing the n-th number only holds when the surrounding words match;
        # otherwise the figures may describe entirely different things.
        self.assertEqual(numeric_changes("营业收入为100亿元。", "净利润为200亿元，另有说明。"), [])

    def test_identical_paragraphs_yield_no_changes(self):
        self.assertEqual(numeric_changes("营业收入为100亿元。", "营业收入为100亿元。"), [])

    def test_division_by_zero_is_survivable(self):
        changes = numeric_changes("上期金额为0元。", "上期金额为500元。")
        self.assertIsNone(changes[0]["delta_ratio"])


class SectionDiffTests(unittest.TestCase):
    def _items(self, section, texts, start_page=1):
        return [{"page": start_page + index, "section": section, "text": text,
                 "key": text.replace(" ", "")} for index, text in enumerate(texts)]

    def test_added_and_removed_paragraphs_are_reported_with_pages(self):
        left = self._items("第三节 风险因素", ["原有段落甲", "原有段落乙"])
        right = self._items("第三节 风险因素", ["原有段落甲", "新增段落丙"])
        results = list(diff_section(left, right))
        kinds = {item["diff_kind"] for item in results}
        self.assertTrue({"added", "removed"} & kinds)
        for item in results:
            if item["diff_kind"] == "added":
                self.assertIsNotNone(item["right_page"])

    def test_edited_paragraph_becomes_a_numeric_difference(self):
        left = self._items("第六节", ["报告期内公司营业收入为241.78亿元，具体情况参见下表。"])
        right = self._items("第六节", ["报告期内公司营业收入为617.99亿元，具体情况参见下表。"])
        results = list(diff_section(left, right))
        self.assertEqual([item["diff_kind"] for item in results], ["numeric"])
        self.assertEqual(results[0]["old_value"], "241.78")

    def test_unchanged_sections_produce_nothing(self):
        same = self._items("第一节", ["完全相同的一段披露内容。"])
        self.assertEqual(list(diff_section(same, same)), [])

    def test_only_sections_present_in_both_versions_are_aligned(self):
        left = self._items("第一节", ["甲"]) + self._items("第二节", ["乙"])
        right = self._items("第二节", ["乙"]) + self._items("第九节", ["丙"])
        self.assertEqual(align_sections(left, right), ["第二节"])


class StageOrderTests(unittest.TestCase):
    def test_disclosure_stages_sort_in_regulatory_order(self):
        titles = ["注册稿_招股说明书", "申报稿_招股说明书", "上会稿_招股说明书"]
        self.assertEqual(sorted(titles, key=stage_rank),
                         ["申报稿_招股说明书", "上会稿_招股说明书", "注册稿_招股说明书"])

    def test_unknown_titles_sort_last(self):
        self.assertGreater(stage_rank("某个未知文件"), stage_rank("注册稿"))

    def test_an_updated_draft_ranks_after_the_original_not_beside_it(self):
        # The stage names nest: "申报稿" is inside "更新申报稿".  Scanning in
        # sequence order gave both rank 0, and the chain then sorted only by
        # publication date -- correct here by luck, wrong the moment two stages
        # share a date.  It stayed hidden while the updated draft was an HTML
        # mirror the comparison dropped for having no page anchors.
        self.assertLess(stage_rank("申报稿_招股说明书"), stage_rank("更新申报稿_招股说明书"))
        self.assertLess(stage_rank("更新申报稿_招股说明书"), stage_rank("上会稿_招股说明书"))
        self.assertLess(stage_rank("上会稿_招股说明书"), stage_rank("注册稿_招股说明书"))

    def test_every_named_stage_gets_its_own_rank(self):
        ranks = [stage_rank(stage) for stage in STAGE_ORDER]
        self.assertEqual(ranks, list(range(len(STAGE_ORDER))))

    def test_an_unnamed_filing_sorts_last(self):
        self.assertEqual(stage_rank("某份没有阶段名的文件"), len(STAGE_ORDER))
        self.assertEqual(stage_rank(None), len(STAGE_ORDER))


class StaleDiffTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, company_id TEXT, document_type TEXT)")
        db.execute("""CREATE TABLE disclosure_diffs(diff_id TEXT PRIMARY KEY, company_id TEXT,
                      left_version_id TEXT, right_version_id TEXT)""")
        db.executemany("INSERT INTO document_versions VALUES(?,?,?)",
                       [("v1", "cxmt", "prospectus"), ("v2", "cxmt", "prospectus"),
                        ("v3", "cxmt", "prospectus"), ("n1", "cxmt", "inquiry_reply")])
        return db

    def test_pairs_from_a_wider_earlier_chain_are_removed(self):
        # An earlier run compared v1→v2 against an HTML mirror; the current chain is v1→v3.
        db = self._db()
        db.executemany("INSERT INTO disclosure_diffs VALUES(?,?,?,?)",
                       [("d1", "cxmt", "v1", "v2"), ("d2", "cxmt", "v2", "v3"), ("d3", "cxmt", "v1", "v3")])
        self.assertEqual(drop_stale_diffs(db, "cxmt", "prospectus"), 3)
        self.assertEqual(db.execute("SELECT count(*) FROM disclosure_diffs").fetchone()[0], 0)

    def test_other_document_types_and_companies_are_untouched(self):
        db = self._db()
        db.executemany("INSERT INTO disclosure_diffs VALUES(?,?,?,?)",
                       [("d1", "cxmt", "v1", "v2"), ("d2", "cxmt", "n1", "n1"),
                        ("d3", "wuxi_apptec", "w1", "w2")])
        self.assertEqual(drop_stale_diffs(db, "cxmt", "prospectus"), 1)
        self.assertEqual({row[0] for row in db.execute("SELECT diff_id FROM disclosure_diffs")}, {"d2", "d3"})

    def test_company_without_versions_deletes_nothing(self):
        db = self._db()
        db.execute("INSERT INTO disclosure_diffs VALUES('d1','cxmt','v1','v2')")
        self.assertEqual(drop_stale_diffs(db, "unknown_company", "prospectus"), 0)
        self.assertEqual(db.execute("SELECT count(*) FROM disclosure_diffs").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()


class MetricNamingTests(unittest.TestCase):
    def test_the_trailing_word_run_names_the_metric(self):
        changes = numeric_changes("美光科技营业总收入251.11亿美元，净利润7.78亿美元。",
                                  "美光科技营业总收入341.32亿美元，净利润85.39亿美元。")
        self.assertEqual([item["metric"] for item in changes],
                         ["美光科技营业总收入", "净利润"])

    def test_a_run_together_table_row_still_names_its_metric(self):
        # The window ends mid-number here, so only the words after the last digit
        # can be trusted as a name.
        self.assertEqual(metric_name("资产总额30287397.18合计"), "合计")

    def test_a_window_that_names_nothing_yields_none(self):
        self.assertIsNone(metric_name("30287397.1827159883."))
        self.assertIsNone(metric_name(""))

    def test_a_trailing_copula_is_dropped(self):
        self.assertEqual(metric_name("公司营业收入为"), "公司营业收入")

    def test_a_single_character_name_is_not_a_metric(self):
        self.assertIsNone(metric_name("2025年"))


class NonFinancialFigureTests(unittest.TestCase):
    def test_a_reporting_period_rollover_is_not_a_figure_change(self):
        # 截至2025年6月末 -> 截至2025年12月末 is a real edit, but it belongs in the
        # paragraph diff, not in a table of 科目 values.
        self.assertEqual(numeric_changes("截至2025年6月末，公司持有房产。",
                                         "截至2025年12月末，公司持有房产。"), [])

    def test_a_document_number_is_not_a_figure_change(self):
        self.assertEqual(numeric_changes("德师报（审）字（25）第S00751号审计报告。",
                                         "德师报（审）字（25）第S00141号审计报告。"), [])

    def test_an_ordinal_reference_is_not_a_figure_change(self):
        self.assertEqual(numeric_changes("详见本节第36页所述。", "详见本节第42页所述。"), [])

    def test_a_measurement_next_to_a_date_still_counts(self):
        changes = numeric_changes("截至2025年6月末，在册员工1000人。",
                                  "截至2025年12月末，在册员工1200人。")
        self.assertEqual([(item["metric"], item["old_value"]) for item in changes],
                         [("在册员工", "1000")])


class FigureSanityTests(unittest.TestCase):
    def test_a_connective_is_not_a_metric_name(self):
        # 增长至2029年的2045亿美元 leaves 年的 in front of the figure.
        self.assertIsNone(metric_name("增长至2029年的"))

    def test_a_run_together_row_is_not_a_measurement(self):
        self.assertEqual(numeric_changes("在册员工153001385896058303人。",
                                         "在册员工19298138589605人。"), [])

    def test_a_leading_zero_fragment_is_not_a_measurement(self):
        self.assertEqual(numeric_changes("比例0047占比。", "比例0091占比。"), [])

    def test_a_decimal_below_one_is_still_a_measurement(self):
        changes = numeric_changes("毛利率为0.35。", "毛利率为0.47。")
        self.assertEqual(changes[0]["old_value"], "0.35")

    def test_a_bare_unit_is_not_a_metric_name(self):
        self.assertIsNone(metric_name("单位：万元"))
        self.assertIsNone(metric_name("以及"))


class OrdinalAndReferenceTests(unittest.TestCase):
    def test_a_renumbered_list_item_is_not_a_figure_change(self):
        # 「7、无控股股东…」 becomes 「8、…」 when an item is inserted above it.
        self.assertEqual(numeric_changes("7、无控股股东和实际控制人的风险，持股21.67%。",
                                         "8、无控股股东和实际控制人的风险，持股21.67%。"), [])

    def test_a_bracketed_list_ordinal_is_not_a_figure_change(self):
        self.assertEqual(numeric_changes("（3）2024年长鑫芯瑞成立。",
                                         "（2）2024年长鑫芯瑞成立。"), [])

    def test_a_bracketed_reference_number_is_not_a_figure_change(self):
        self.assertEqual(numeric_changes("德师报（审）字（25）第S00751号。",
                                         "德师报（审）字（26）第S00141号。"), [])

    def test_a_figure_after_an_ordinal_still_counts(self):
        changes = numeric_changes("1、研发人员4653名。", "1、研发人员6259名。")
        self.assertEqual([item["old_value"] for item in changes], ["4653"])


class PeriodRolloverTests(unittest.TestCase):
    def test_a_moved_cut_off_date_is_a_rollover(self):
        self.assertTrue(is_period_rollover("截至2025年6月30日，共3,116项。",
                                           "截至2025年12月31日，共3,929项。"))

    def test_a_restatement_under_the_same_period_is_not(self):
        # This is the case the difference table exists for: same period, new figure.
        self.assertFalse(is_period_rollover("截至2025年6月30日，共3,116项。",
                                            "截至2025年6月30日，共3,929项。"))

    def test_a_fiscal_year_and_a_year_range_both_count_as_periods(self):
        self.assertTrue(is_period_rollover("2024财年，净利润344,513.51亿。",
                                           "2025财年，净利润452,068.05亿。"))
        self.assertTrue(is_period_rollover("2024-2029年搭载量为1,972。",
                                           "2025-2030年搭载量为1,916。"))

    def test_the_anchors_are_read_out_of_the_prose(self):
        self.assertEqual(period_anchors("截至2025年6月30日的数据"), {"2025年6月30日"})

    def test_a_paragraph_with_no_dates_is_not_a_rollover(self):
        self.assertFalse(is_period_rollover("营业收入为241.78亿元。", "营业收入为617.99亿元。"))


class LineJoinTests(unittest.TestCase):
    """The separator that was there before ``.strip()`` removed it.

    Rejoining wrapped lines with nothing is right for Chinese and wrong for
    everything else, and the difference had been silently welding words and
    figures together for as long as the index has existed.
    """

    def test_chinese_prose_rejoins_with_no_space(self):
        # The case this was written for, and it stays correct.
        self.assertEqual(join_gap("公司拟募集资金", "总额为二百九十五亿元"), "")

    def test_english_prose_keeps_the_space_it_arrived_with(self):
        # 'subject to the ownership reporting ' is stripped to '…reporting' and
        # welded onto 'requirements…' — a word in neither line, then indexed
        # and searched as if it were one.
        self.assertEqual(join_gap("subject to the ownership reporting", "requirements of"), " ")

    def test_two_table_figures_are_not_fused_into_a_third(self):
        # 610,000.00 above 231,920.20 became 610,000.00231,920.20, a number
        # that appears in no table and in no filing.
        self.assertEqual(join_gap("610,000.00", "231,920.20"), " ")

    def test_a_figure_after_chinese_is_still_separated(self):
        self.assertEqual(join_gap("注册资本", "6,019,279.74"), " ")

    def test_chinese_punctuation_counts_as_chinese(self):
        # A clause ending in ，or （ wraps without a space like any other Han
        # character; treating the punctuation as Latin would insert one.
        self.assertEqual(join_gap("本次发行不存在，", "股东公开发售"), "")
        self.assertEqual(join_gap("人民币普通股（", "A 股）"), "")

    def test_an_empty_side_adds_nothing(self):
        self.assertEqual(join_gap("", "anything"), "")
        self.assertEqual(join_gap("anything", ""), "")

    def test_the_paragraph_builder_uses_it(self):
        # The lines have to clear MIN_PARAGRAPH_CHARS or paragraphs() drops
        # them and the assertion passes on an empty string — which is what the
        # first version of this test did, and it survived reverting the call
        # site to `+= line`. A test against silent failure that itself passes
        # silently is worse than no test.
        text = ("[PAGE 1]\n本次增资投资者的增资情况如下所示，金额单位为人民币元：\n"
                "610,000.00\n231,920.20\n以上金额已经会计师事务所审验并出具验资报告。\n")
        joined = " ".join(item["text"] for item in paragraphs(text))
        self.assertTrue(joined, "fixture was filtered out before the assertion ran")
        self.assertIn("610,000.00", joined)
        self.assertNotIn("610,000.00231,920.20", joined)

    def test_whitespace_does_not_reach_the_comparison_key(self):
        # Diff keys strip whitespace, so adding separators cannot make an
        # unchanged paragraph read as an edit — the stored comparisons stay
        # valid without a rebuild.
        self.assertEqual(normalize_paragraph("610,000.00 231,920.20"),
                         normalize_paragraph("610,000.00231,920.20"))

    def test_no_separator_is_inserted_inside_a_bracket(self):
        # A line breaking right after 「（」 had no space there either.
        self.assertEqual(join_gap("人民币普通股（", "A 股）"), "")
        # The mark has to be *at* the join. A closing bracket later in the line
        # says nothing about the break: 295 | 亿元） is a digit meeting a Han
        # character and correctly takes a space.
        self.assertEqual(join_gap("超额配售选择权", "）之前"), "")
        self.assertEqual(join_gap("募集资金 295", "亿元）"), " ")

    def test_a_percent_sign_stays_on_its_number(self):
        self.assertEqual(join_gap("增长 24.2", "%"), "")
