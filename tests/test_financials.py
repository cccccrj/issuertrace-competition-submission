"""主要会计数据 out of the periodic reports.

The parser walks a table that arrives one cell per line — except where it does
not. Every assertion here defends against a specific way this produced a wrong
number rather than no number, which is the only outcome worse than leaving the
reports unread.
"""
from __future__ import annotations

import sqlite3
import unittest

from processor.financials import (METRICS, check_cumulative, clean_label, ensure_schema,
                                  is_prefix, parse_indicators, period_of)

# The shape most filings use: one cell per line, labels wrapped mid-word.
ONE_CELL_PER_LINE = """
3、公司主要会计数据和财务指标
3.1 近3 年的主要会计数据和财务指标
单位：元
币种：人民币
2025年
2024年
本年比上年
增减(%)
2023年
总资产
103,120,968,579.11
80,325,824,408.75
28.38
73,669,349,307.12
归属于上市公司股东
的净资产
79,712,290,246.44
58,632,715,174.37
35.95
55,122,453,910.73
营业收入
45,456,165,774.18
39,241,431,359.88
15.84
40,340,806,877.04
加权平均净资产收益
率（%）
28.56
16.78
增加11.78个
百分点
18.99
基本每股收益（元／
股）
6.70
3.28
104.27
3.27
3.2 报告期分季度的主要会计数据
营业收入
9,654,595,304.28
11,144,686,578.18
"""

# The shape that broke it: two cells share a line.
TWO_CELLS_PER_LINE = """
主要会计数据和财务指标
3.1 近3 年的主要会计数据和财务指标
2023年
2022年
本年比上年
增减(%)
2021年
总资产
73,669,349,307.12 64,690,326,746.96
13.88 55,127,388,384.03
营业收入
40,340,806,877.04 39,354,777,805.53
2.51
22,902,385,129.11
3.2 报告期分季度的主要会计数据
营业收入
8,963,741,607.53
9,907,543,890.09
"""

# Two sub-tables under one heading, separated by their own column headers.
TWO_SUBTABLES = """
主要财务数据
本报告期末
上年度末
本报告期末比上年
度末增减(%)
总资产
468,077,863,007.24
336,784,922,216.18
38.98
本报告期
上年同期
本报告期比上年同
期增减(%)
营业收入
150,310,076,758.00
15,437,924,611.51
873.64
利润总额
117,145,441,342.17
-4,086,539,257.89
不适用
"""


# A filing whose headline table this parser cannot read — the revenue row is
# absent from 3.1 — with a clean per-quarter table underneath it. Nothing here
# should come back with a revenue figure at all.
HEADLINE_TABLE_MISSING_REVENUE = """
3.1 近3 年的主要会计数据和财务指标
总资产
103,120,968,579.11
80,325,824,408.75
3.2 报告期分季度的主要会计数据
第一季度
（1-3 月份）
营业收入
9,654,595,304.28
11,144,686,578.18
归属于上市公司股东的净利润
3,671,960,950.47
"""

# The header block that sits between two sub-tables, immediately after a label
# with no values of its own — the shape that made a header swallow the row
# beneath it.
HEADER_AFTER_LABEL = """
主要财务数据
营业收入
本报告期
上年同期
本报告期比上年同
期增减(%)
利润总额
117,145,441,342.17
"""


class LabelTests(unittest.TestCase):
    def test_a_label_split_across_lines_is_rejoined(self):
        self.assertEqual(clean_label(["归属于上市公司股东", "的净资产"]),
                         "归属于上市公司股东的净资产")

    def test_a_unit_note_is_cut_even_when_it_is_itself_split(self):
        # 「基本每股收益（元」 / 「／股）」 arrives as two lines; a rule that only
        # removed the complete 「（元／股）」 matched neither half, and EPS came
        # back missing from every annual report.
        self.assertEqual(clean_label(["基本每股收益（元", "／股）"]), "基本每股收益")
        self.assertEqual(clean_label(["加权平均净资产收益", "率（%）"]), "加权平均净资产收益率")

    def test_a_column_header_is_not_a_prefix_of_any_metric(self):
        # This is what stops the header between two sub-tables from swallowing
        # the row beneath it. Without it 营业收入 was found in 1 report of 12.
        for header in ("本报告期", "上年同期", "本报告期比上年同期增减", "2025年"):
            self.assertFalse(is_prefix(header), header)

    def test_a_partial_metric_name_is_a_prefix(self):
        self.assertTrue(is_prefix("归属于上市公司股东"))
        self.assertTrue(is_prefix("经营活动产生的"))


class PeriodTests(unittest.TestCase):
    def test_the_period_comes_from_the_title_not_the_filing_date(self):
        # An annual report is filed the following March; taking the period from
        # published_at would be off by a year for every one of them.
        self.assertEqual(period_of("2025年年度报告摘要"), ("2025", "annual"))
        self.assertEqual(period_of("2026年半年度报告摘要"), ("2026H1", "interim"))
        self.assertEqual(period_of("2026年第一季度报告"), ("2026Q1", "quarterly"))
        self.assertEqual(period_of("2025年第三季度报告"), ("2025Q3", "quarterly"))

    def test_a_report_that_is_not_a_financial_one_has_no_period(self):
        # 可持续发展报告摘要 matches the document filter and carries no such table.
        self.assertIsNone(period_of("2025年度可持续发展报告摘要"))


class ParseTests(unittest.TestCase):
    def test_one_cell_per_line_is_read(self):
        found = parse_indicators(ONE_CELL_PER_LINE)
        self.assertEqual(found["revenue"], ("CNY", 45456165774.18))
        self.assertEqual(found["total_assets"], ("CNY", 103120968579.11))
        self.assertEqual(found["equity_attributable"], ("CNY", 79712290246.44))

    def test_two_cells_on_one_line_are_read_and_only_the_first_is_taken(self):
        # 「73,669,349,307.12 64,690,326,746.96」 is this year and last year on
        # one line. A pattern anchored to a whole line matched neither.
        found = parse_indicators(TWO_CELLS_PER_LINE)
        self.assertEqual(found["total_assets"], ("CNY", 73669349307.12))
        self.assertEqual(found["revenue"], ("CNY", 40340806877.04))

    def test_the_per_quarter_table_below_is_never_read(self):
        # Both fixtures repeat 营业收入 under 3.2 with a quarter's figure. That
        # is how 2023 revenue was briefly stored as 89.64亿 instead of 403.41亿
        # — wrong by a factor of four and entirely plausible in a column.
        for text in (ONE_CELL_PER_LINE, TWO_CELLS_PER_LINE):
            self.assertNotEqual(parse_indicators(text)["revenue"][1], 9654595304.28)
            self.assertNotEqual(parse_indicators(text)["revenue"][1], 8963741607.53)

    def test_a_broken_headline_table_yields_nothing_rather_than_a_quarter(self):
        # Defence in depth behind first-match-wins: if the headline row cannot
        # be read for any reason, the parser must not quietly answer with the
        # per-quarter figure sitting below it under the same name.
        found = parse_indicators(HEADLINE_TABLE_MISSING_REVENUE)
        self.assertNotIn("revenue", found)
        self.assertNotIn("net_profit_attributable", found)
        self.assertEqual(found["total_assets"], ("CNY", 103120968579.11))

    def test_a_header_block_cannot_be_glued_onto_the_label_before_it(self):
        # 营业收入 here has no values; the column headers follow it directly.
        # Accumulating them onto the label would make the *next* row's figure
        # come back as revenue.
        found = parse_indicators(HEADER_AFTER_LABEL)
        self.assertNotIn("revenue", found)
        self.assertEqual(found["profit_before_tax"], ("CNY", 117145441342.17))

    def test_a_percentage_and_a_per_share_figure_keep_their_own_units(self):
        found = parse_indicators(ONE_CELL_PER_LINE)
        self.assertEqual(found["weighted_roe"], ("percent", 28.56))
        self.assertEqual(found["basic_eps"], ("CNY_per_share", 6.70))

    def test_two_sub_tables_under_one_heading_are_both_read(self):
        # The interim form puts balance-sheet items and income items in separate
        # blocks with their own column headers in between.
        found = parse_indicators(TWO_SUBTABLES)
        self.assertEqual(found["total_assets"], ("CNY", 468077863007.24))
        self.assertEqual(found["revenue"], ("CNY", 150310076758.00))

    def test_a_negative_comparative_does_not_become_the_value(self):
        # 上年同期 is negative for a company that had just listed; the current
        # column is still the first one.
        self.assertEqual(parse_indicators(TWO_SUBTABLES)["profit_before_tax"],
                         ("CNY", 117145441342.17))

    def test_text_without_the_table_yields_nothing_rather_than_guessing(self):
        self.assertEqual(parse_indicators("公司发布了一份可持续发展报告，内容如下。"), {})

    def test_every_metric_name_has_a_display_label(self):
        from processor.financials import METRIC_LABELS
        for stored, _ in METRICS.values():
            self.assertIn(stored, METRIC_LABELS, stored)


class ConsistencyTests(unittest.TestCase):
    def _db(self, rows):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        for period, value in rows:
            db.execute("""INSERT INTO financial_indicators(company_id,period,period_type,metric,
                value,unit,version_id,extracted_at) VALUES('c',?,'x','revenue',?,'CNY','v','now')""",
                (period, value))
        db.commit()
        return db

    def test_revenue_that_shrinks_across_the_year_is_reported(self):
        # The shape of the bug this parser actually had: a quarter's figure
        # wearing the year's label.
        db = self._db([("2023Q1", 8_963_741_607.0), ("2023H1", 18_871_285_497.0),
                       ("2023", 8_963_741_607.0)])
        self.assertEqual(len(check_cumulative(db)), 1)

    def test_a_properly_ordered_year_reports_nothing(self):
        db = self._db([("2025Q1", 9_654_595_304.0), ("2025H1", 20_799_281_882.0),
                       ("2025", 45_456_165_774.0)])
        self.assertEqual(check_cumulative(db), [])

    def test_a_year_with_only_one_figure_is_not_a_problem(self):
        db = self._db([("2026H1", 28_897_478_718.0)])
        self.assertEqual(check_cumulative(db), [])


if __name__ == "__main__":
    unittest.main()
