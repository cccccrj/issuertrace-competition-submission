"""The display names the browser uses must be the ones the server knows about.

`api/labels.py` says a code reaching a page without a label 「是一个 bug，表现为
本该给委员会看的文件里出现英文缩写」. That was true of the *server's* renderings,
which the file's own test covers. Nothing covered the browser's, and the two had
drifted a long way:

* `ISSUE_LABELS` was missing five codes, including all four added when the
  vocabulary went to v2 for the 84-company dataset — so 行业政策 rendered as
  `INDUSTRY_POLICY`.
* `PRODUCTION_LABELS` had four of eight modes and carried an `ORIGINAL` key that
  is not in the enum at all. That is the same stale key already found and fixed
  once in `alerts.py`'s `MODE_CREDIBILITY`; it had a twin here and nobody looked.
  生产方式 is the conclusion of 辨伪 — task three's whole output — and half of it
  was showing in English.
* `EVENT_TYPE_LABELS` and `IMPACT_OBJECT_LABELS` did not exist in the front end,
  so two of the brief's seven required outputs displayed as raw enums on the page
  the 功能大纲 calls 「答辩演示的核心页面」.

Duplicating the maps is a deliberate trade: the alternative is a request before
the first paint on every detail page. The duplication is only safe with this
test, so the test is the thing that makes the trade legitimate.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from api.labels import (
    ARTICLE_SENTIMENT_LABELS,
    CLAIM_TYPE_LABELS,
    EVENT_TYPE_LABELS,
    IMPACT_DIRECTION_LABELS,
    IMPACT_MAGNITUDE_LABELS,
    IMPACT_OBJECT_LABELS,
    ISSUE_CODE_LABELS,
    MAGNITUDE_BASIS_LABELS,
    PRODUCTION_MODE_LABELS,
    CREDIBILITY_LABELS,
    EDGE_KIND_LABELS,
    RISK_LEVEL_LABELS,
    REVIEW_REASON_LABELS,
    RISK_PART_LABELS,
    UNVERIFIABLE_SUBTYPE_LABELS,
)

API_JS = Path(__file__).resolve().parent.parent / "web" / "src" / "api.js"

PAIRS = {
    "ISSUE_LABELS": ISSUE_CODE_LABELS,
    "PRODUCTION_LABELS": PRODUCTION_MODE_LABELS,
    "EVENT_TYPE_LABELS": EVENT_TYPE_LABELS,
    "IMPACT_OBJECT_LABELS": IMPACT_OBJECT_LABELS,
    "ARTICLE_SENTIMENT_LABELS": ARTICLE_SENTIMENT_LABELS,
    # 2026-09-06 起的五张：方向/强度此前前后端各写一份且已经漂了（前端缺 uncertain、
    # moderate 写作「中等」），另三张是这一轮新展示的依据类词表。
    "CLAIM_TYPE_LABELS": CLAIM_TYPE_LABELS,
    "IMPACT_DIRECTION_LABELS": IMPACT_DIRECTION_LABELS,
    "IMPACT_MAGNITUDE_LABELS": IMPACT_MAGNITUDE_LABELS,
    "MAGNITUDE_BASIS_LABELS": MAGNITUDE_BASIS_LABELS,
    "RISK_PART_LABELS": RISK_PART_LABELS,
    "REVIEW_REASON_LABELS": REVIEW_REASON_LABELS,
    # 两侧的名字不同：浏览器那边同时还有一张 CREDIBILITY_LABELS（「可信度高」
    # 这种带量名的写法），格子里用的是短的那张。名字对不上不要紧，这张表要求
    # 的是内容一致——研判档案此前把后端的 medium 原样打给了读者。
    "CREDIBILITY_SHORT": CREDIBILITY_LABELS,
    # 风险简报此前把 high 原样印进了下发材料：「风险 high（100.0）」。
    "RISK_LEVEL_LABELS": RISK_LEVEL_LABELS,
    "EDGE_KIND_LABELS": EDGE_KIND_LABELS,
    "UNVERIFIABLE_SUBTYPE_LABELS": UNVERIFIABLE_SUBTYPE_LABELS,
}


def javascript_map(name: str) -> dict[str, str]:
    source = API_JS.read_text(encoding="utf-8")
    block = re.search(rf"export const {name} = \{{(.*?)\n\}}", source, re.S)
    if not block:
        raise AssertionError(f"web/src/api.js 里没有 {name}")
    return dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'([^']*)'", block.group(1)))


class LabelParityTests(unittest.TestCase):
    def test_every_code_the_server_knows_has_a_label_in_the_browser(self):
        for name, mapping in PAIRS.items():
            with self.subTest(name):
                missing = set(mapping) - set(javascript_map(name))
                self.assertFalse(missing, f"{name} 前端缺少：{sorted(missing)}")

    def test_the_browser_invents_no_codes_of_its_own(self):
        # A label for a code the enum does not contain is dead weight that reads
        # as coverage — `ORIGINAL` sat in PRODUCTION_LABELS long after the enum
        # dropped it, so the map looked fuller than it was.
        for name, mapping in PAIRS.items():
            with self.subTest(name):
                extra = set(javascript_map(name)) - set(mapping)
                self.assertFalse(extra, f"{name} 前端多出后端没有的码：{sorted(extra)}")

    def test_the_processing_layer_names_the_issues_the_same_way(self):
        # 预警标题在处理层生成，而处理层不能 import api，所以议题短名在
        # processor/pipeline.py 里有第二份。重复是自觉的取舍——这条测试是让它成立
        # 的东西：两侧一旦不一致，事项标题上的专题名就会和筛选器里的对不上。
        from processor.pipeline import ISSUE_LABELS
        self.assertEqual(ISSUE_LABELS, ISSUE_CODE_LABELS)

    def test_the_propagation_layer_names_the_edges_the_same_way(self):
        # 标签的键必须正好是传播层的那九类：多一个是死重量，少一个会让某一行
        # 在报告里打出英文码来。
        from processor.propagation import EDGE_KINDS
        self.assertEqual(set(EDGE_KIND_LABELS), set(EDGE_KINDS))

    def test_the_disposition_states_have_one_name_each(self):
        # 处置状态在浏览器那边带图标与颜色，是嵌套对象，PAIRS 那条正则读不了它。
        # 「待上报」改成「待上级复核」时，就是这一张表最容易只改一边。
        from api.dispositions import STATUSES
        source = API_JS.read_text(encoding="utf-8")
        block = re.search(r"export const DISPOSITION_STATUS_META = \{(.*?)\n\}", source, re.S)
        browser = dict(re.findall(r"([a-z_]+): \{ label: '([^']*)'", block.group(1)))
        self.assertEqual(browser, {code: meta["label"] for code, meta in STATUSES.items()})

    def test_the_two_sides_say_the_same_thing(self):
        for name, mapping in PAIRS.items():
            with self.subTest(name):
                self.assertEqual(javascript_map(name), dict(mapping))


if __name__ == "__main__":
    unittest.main()
