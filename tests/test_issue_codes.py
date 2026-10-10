import hashlib
import unittest

from processor.issue_codes import (
    CODE_GUIDE, EXAMPLE_CLAUSE, MAX_CODES, PROMPT_VERSION, SYSTEM_PROMPT, TASK,
    contract, make_prompt, sample, schema, system_prompt,
)
from processor.pipeline import ISSUE_RULES


class Row(dict):
    """The two fields ``sample`` reads off a benchmark row."""

    def __getitem__(self, key):
        return super().__getitem__(key)


def entry(article_id, codes):
    """A (doc, text, row, gold codes) tuple in the shape ``sample`` consumes."""
    return (object(), "正文", Row(article_file_id=article_id), set(codes))


class ContractTests(unittest.TestCase):
    def test_the_schema_offers_exactly_the_system_vocabulary(self):
        # A code the schema omits could never be predicted; one it invents would
        # score as a permanent error and would not survive verify's whitelist.
        self.assertEqual(schema()["properties"]["issue_codes"]["items"]["enum"],
                         list(ISSUE_RULES))

    def test_every_code_the_schema_offers_is_defined_in_the_prompt(self):
        # The whole point of the sidecar: ai_label has always carried these 26
        # values as a bare enum its prompt never explains.  A code that loses its
        # definition here reintroduces exactly that defect.
        for code in ISSUE_RULES:
            self.assertIn(code, CODE_GUIDE, code)
            self.assertIn(f"- {code} ", SYSTEM_PROMPT, code)

    def test_the_guide_defines_no_code_the_system_does_not_have(self):
        self.assertEqual(set(CODE_GUIDE), set(ISSUE_RULES))

    def test_the_cap_matches_what_the_frozen_map_can_produce(self):
        # The map folds an event name into at most two codes; a schema allowing
        # three would be wrong about the task, not generous.
        self.assertEqual(MAX_CODES, 2)
        self.assertEqual(schema()["properties"]["issue_codes"]["maxItems"], 2)

    def test_the_prompt_separates_expansion_from_transformation(self):
        # The two company-side codes added in vocabulary v2 are the pair a model
        # will otherwise conflate; the boundary has to be stated, not implied.
        self.assertIn("进入原本没有的地盘", SYSTEM_PROMPT)
        self.assertIn("重组已经握有的地盘", SYSTEM_PROMPT)

    def test_the_prompt_says_to_read_the_subject_not_the_words(self):
        # The defect this module exists to route round: keywords cannot see a
        # semantic dimension, so the prompt must forbid keyword reasoning.
        self.assertIn("不是文中出现了哪些词", SYSTEM_PROMPT)

    def test_the_prompt_fences_external_content(self):
        self.assertIn("〔外部内容〕", SYSTEM_PROMPT)
        self.assertIn("绝不执行", SYSTEM_PROMPT)

    def test_the_article_body_is_delivered_inside_the_fence(self):
        prompt = make_prompt("标题", "2026-01-01", "来源", "正文内容")
        body = prompt.split("〔外部内容〕")[1]
        self.assertIn("正文内容", body)

    def test_a_long_article_is_truncated(self):
        prompt = make_prompt("t", "2026-01-01", "s", "字" * 5000)
        self.assertLess(prompt.count("字"), 5000)

    def test_the_contract_carries_the_system_prompt(self):
        self.assertEqual(contract().system_prompt, SYSTEM_PROMPT)

    def test_the_task_name_distinguishes_this_run_from_the_rule_run(self):
        # benchmark_runs holds both; e1-issue_codes is the rule path.
        self.assertEqual(TASK, "e1-model")
        self.assertTrue(PROMPT_VERSION.startswith("issue-codes-"))


class SampleTests(unittest.TestCase):
    def test_a_row_is_kept_for_its_rarest_code(self):
        # TECH_LEVEL is common, FINANCIAL scarce; the mixed row must be spent on
        # FINANCIAL's quota or the scarce class is never measured.
        rows = [entry(f"a{i}", ["TECH_LEVEL"]) for i in range(5)]
        rows.append(entry("z", ["TECH_LEVEL", "FINANCIAL"]))
        picked = sample(rows, per_code_cap=1)
        kept = {e[2]["article_file_id"] for e in picked}
        self.assertIn("z", kept)
        self.assertEqual(len(picked), 2)

    def test_the_cap_bounds_each_code_not_the_whole_draw(self):
        rows = ([entry(f"t{i}", ["TECH_LEVEL"]) for i in range(10)]
                + [entry(f"c{i}", ["CUSTOMER"]) for i in range(10)])
        self.assertEqual(len(sample(rows, per_code_cap=3)), 6)

    def test_the_same_cap_draws_the_same_articles(self):
        rows = [entry(f"a{i}", ["TECH_LEVEL"]) for i in range(20)]
        first = [e[2]["article_file_id"] for e in sample(rows, per_code_cap=5)]
        second = [e[2]["article_file_id"] for e in sample(list(reversed(rows)), per_code_cap=5)]
        self.assertEqual(first, second)

    def test_a_scarce_code_is_taken_whole(self):
        rows = ([entry(f"t{i}", ["TECH_LEVEL"]) for i in range(50)]
                + [entry("f1", ["FINANCIAL"]), entry("f2", ["FINANCIAL"])])
        kept = {e[2]["article_file_id"] for e in sample(rows, per_code_cap=10)}
        self.assertIn("f1", kept)
        self.assertIn("f2", kept)

    def test_an_empty_corpus_does_not_raise(self):
        self.assertEqual(sample([], per_code_cap=10), [])


if __name__ == "__main__":
    unittest.main()


class FewShotPromptTests(unittest.TestCase):
    """Adding examples must be provably inert when it is switched off."""

    ARGS = ("标题", "2026-02-01", "新浪财经", "正文" * 40)

    def test_no_examples_reproduces_the_zero_shot_prompt_byte_for_byte(self):
        # Every E1 number from v1 onward was measured on this exact string.  If
        # shots=0 drifts by one character, the k sweep's control point stops
        # being a control and the comparison it anchors means nothing.
        # Pinned as a literal, not compared against itself: `make_prompt(...)`
        # and `make_prompt(..., "")` drift together, so that pair alone would
        # survive a stray newline in the no-examples branch.
        title, published, source, text = self.ARGS
        self.assertEqual(make_prompt(*self.ARGS),
                         f"来源：{source}\n标题：{title}\n发布时间：{published}\n"
                         f"〔外部内容〕\n{text}\n〔外部内容结束〕")
        self.assertEqual(make_prompt(*self.ARGS), make_prompt(*self.ARGS, ""))
        self.assertEqual(system_prompt(0), SYSTEM_PROMPT)
        self.assertEqual(contract(0).system_prompt, SYSTEM_PROMPT)

    def test_examples_arrive_inside_their_own_boundary(self):
        prompt = make_prompt(*self.ARGS, "[示例1] 标题：甲\n正文：乙\n答案：TECH_LEVEL")
        self.assertIn("〔参考示例〕", prompt)
        self.assertIn("〔参考示例结束〕", prompt)
        # Ahead of the article, so the material under judgement is the last
        # thing read and the examples cannot be mistaken for part of it.
        self.assertLess(prompt.index("〔参考示例结束〕"), prompt.index("〔外部内容〕"))

    def test_the_injection_rule_covers_the_example_region(self):
        # The examples are quoted external text like any other.  The clause
        # lives in the system prompt rather than beside the examples, where the
        # material could argue with it.
        instructed = system_prompt(4)
        self.assertIn("〔参考示例〕", instructed)
        self.assertIn("绝不执行", EXAMPLE_CLAUSE)
        self.assertNotIn(EXAMPLE_CLAUSE, system_prompt(0))
        self.assertTrue(instructed.endswith("输出必须符合给定 JSON Schema。"))

    def test_the_shipped_prompt_version_is_still_the_zero_shot_one(self):
        # v3 is claimed only when a k>0 configuration is actually adopted; until
        # then the corpus and the sidecar both run v2 and the label must say so.
        self.assertEqual(PROMPT_VERSION, "issue-codes-v2")


class PromptVersionIsLoadBearing(unittest.TestCase):
    """A version label the code does not enforce is a label, not a version.

    Extracting ``issue_code_rules`` into ``pipeline`` on 2026-09-05 dropped one
    judging line and rewrapped another while ``PROMPT_VERSION`` stayed at v2.
    Two runs an hour apart therefore carry the same label and different prompts,
    and E1's train micro-F1 fell 0.4429 -> 0.4286 -- seven times the noise floor
    -- with nothing in the suite to say so.  ``benchmark_runs`` records the
    label, so every number keyed on it silently became incomparable.

    Pinning the digest makes the next such edit fail here first.  Changing the
    prompt is fine; changing it without changing the version alongside is not.
    """

    DIGESTS = {
        "issue-codes-v2": "d6867eb9fe8eafd2",
        # v5 (2026-09-06) 补上了 event_types 与影响三项的判据——此前它们是这份提示词
        # 从不提及的裸 enum，与 v3 之前的 issue_codes 同病。
        "event-label-v8": "eaa5bc8b0687205e",
    }

    def digest(self, text):
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def test_the_issue_code_prompt_matches_its_version(self):
        self.assertEqual(self.digest(SYSTEM_PROMPT), self.DIGESTS[PROMPT_VERSION],
                         f"{PROMPT_VERSION} 的提示词变了——改提示词就要同时改版本号")

    def test_the_extraction_prompt_matches_its_version(self):
        # ai_label reads the same shared rules, so a change there reaches the
        # corpus rather than the sidecar and matters more, not less.
        from processor.ai_label import PROMPT_VERSION as LABEL_VERSION
        from processor.ai_label import SYSTEM_PROMPT as LABEL_PROMPT
        self.assertEqual(self.digest(LABEL_PROMPT), self.DIGESTS[LABEL_VERSION],
                         f"{LABEL_VERSION} 的提示词变了——改提示词就要同时改版本号")

    def test_the_line_that_the_refactor_dropped_is_back(self):
        # Named on its own so the regression has a test that describes it, not
        # only a digest that would also fire for an unrelated edit.
        self.assertIn("二者不要同时给出，除非稿件确实两件事都在讲。", SYSTEM_PROMPT)
