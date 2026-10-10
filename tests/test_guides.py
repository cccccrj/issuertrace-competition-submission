"""Every controlled value a model is asked for must be defined where it is asked.

This is the root guard for one recurring defect, stated by ``ai_label`` itself as
"a value nothing defines being one the model fills in by guesswork".  It has been
found and repaired four times — ``production_mode`` (99.9% of the corpus collapsed
into ``BRIEF``), ``article_sentiment`` (equal to ``impact_direction`` on 1,551 of
1,995 rows), ``issue_codes`` (test micro-F1 0.1789 → 0.4211 once definitions and a
precedence chain were supplied), and on 2026-09-06 ``event_types`` plus the impact
triple.  Each repair fixed one vocabulary and left the others; a test that walks
every vocabulary in every prompt is what stops the fifth occurrence.

``EnumsAreExplainedInEveryPrompt`` is therefore the load-bearing test here: it
takes each module's *rendered* prompt and asserts that every value of every enum
that prompt asks for appears in it.  The rest pin the judging sentences that carry
a measured decision, on the precedent of
``tests.test_issue_codes.test_the_line_that_the_refactor_dropped_is_back``: a digest
fires for any edit but never says which line went missing.
"""

import unittest

from processor import ai_label, article_sentiment, event_emotion, issue_codes, pipeline, production_mode


class GuidesCoverTheirVocabularies(unittest.TestCase):

    def test_every_event_type_has_a_definition(self):
        self.assertEqual(set(pipeline.EVENT_TYPE_GUIDE), set(pipeline.EVENT_TYPES))

    def test_every_issue_code_has_a_definition(self):
        self.assertEqual(set(pipeline.CODE_GUIDE), set(pipeline.ISSUE_RULES))

    def test_every_impact_object_has_a_definition(self):
        self.assertEqual(set(pipeline.IMPACT_OBJECT_GUIDE), set(pipeline.IMPACT_OBJECTS))

    def test_the_five_groups_partition_the_twenty_objects(self):
        # The grouping is itself a judging rule — company first, relationships
        # next, spillover last — so a value missing from it is a value with no
        # place in the order.
        grouped = [name for _, members in pipeline.IMPACT_OBJECT_GROUPS for name in members]
        self.assertEqual(len(grouped), len(set(grouped)), "分组里有重复取值")
        self.assertEqual(set(grouped), set(pipeline.IMPACT_OBJECTS))

    def test_direction_and_magnitude_are_defined_value_by_value(self):
        for value in pipeline.IMPACT_DIRECTIONS:
            self.assertIn(value, pipeline.IMPACT_DIRECTION_GUIDE)
        for value in pipeline.IMPACT_MAGNITUDES:
            self.assertIn(value, pipeline.IMPACT_MAGNITUDE_GUIDE)

    def test_every_magnitude_basis_is_defined_and_major_has_three(self):
        self.assertTrue(set(pipeline.MAJOR_BASES) <= set(pipeline.MAGNITUDE_BASES))
        self.assertEqual(len(pipeline.MAJOR_BASES), 3)
        for basis, text in pipeline.MAGNITUDE_BASES.items():
            self.assertTrue(text.strip(), basis)

    def test_every_claim_type_has_a_definition(self):
        # The fifth bare enum, and the one a reader trusts most directly: a claim
        # shown as 「传言」 next to one shown as 「事实」 is asking to be believed by
        # its label.  The rule path records every claim as fact, which is its own
        # separate defect.
        for value in pipeline.CLAIM_TYPES:
            self.assertIn(value, pipeline.CLAIM_TYPE_GUIDE)

    def test_every_production_mode_and_tone_has_a_definition(self):
        for mode in pipeline.PRODUCTION_MODES:
            self.assertIn(mode, pipeline.MODE_GUIDE)
        for tone in article_sentiment.SENTIMENTS:
            self.assertIn(tone, pipeline.TONE_GUIDE)


class EnumsAreExplainedInEveryPrompt(unittest.TestCase):
    """Not "a guide exists" but "the prompt that asks carries it".

    ``issue_code_rules`` spent two days living only in the benchmark sidecar while
    the path the platform actually labels with never received it
    (``pipeline.issue_code_rules``'s docstring records that).  A guide in the
    repository and a prompt without it is the same defect as no guide at all.
    """

    def prompts(self):
        return {
            "ai_label": ai_label.SYSTEM_PROMPT,
            "issue_codes": issue_codes.SYSTEM_PROMPT,
            "production_mode": production_mode.SYSTEM_PROMPT,
            "article_sentiment": article_sentiment.SYSTEM_PROMPT,
            "event_emotion": event_emotion.SYSTEM_PROMPT,
        }

    # Which vocabularies each prompt asks its model to choose from.  A prompt is
    # only held to the enums it actually requests.
    ASKED_FOR = {
        "ai_label": ("EVENT_TYPES", "ISSUE_RULES", "PRODUCTION_MODES", "IMPACT_OBJECTS",
                     "IMPACT_DIRECTIONS", "IMPACT_MAGNITUDES", "CLAIM_TYPES"),
        "issue_codes": ("ISSUE_RULES",),
        "production_mode": ("PRODUCTION_MODES",),
    }

    def test_every_value_a_prompt_asks_for_is_explained_in_it(self):
        prompts = self.prompts()
        for module, vocabularies in self.ASKED_FOR.items():
            prompt = prompts[module]
            for vocabulary in vocabularies:
                values = getattr(pipeline, vocabulary)
                for value in values:
                    with self.subTest(module=module, vocabulary=vocabulary, value=value):
                        self.assertIn(value, prompt,
                                      f"{module} 的提示词里 {value} 是个没有定义的裸取值")

    def test_the_extraction_prompt_carries_the_mode_and_tone_guides_too(self):
        # Both have a dedicated sidecar, but a sidecar only reaches events that
        # already exist: a newly collected document gets its first value here, and
        # announcements (874 events) never enter production_mode's pass at all.
        # This prompt explained two of the eight modes until 2026-09-06, which is
        # the wording that collapsed 99.9% of the first pass into BRIEF.
        self.assertIn(pipeline.MODE_GUIDE, ai_label.SYSTEM_PROMPT)
        self.assertIn(pipeline.TONE_GUIDE, ai_label.SYSTEM_PROMPT)

    def test_the_extraction_prompt_names_the_field_it_is_judging(self):
        # The 12 event types were absent as values *and* as a field name: the word
        # "event_types" did not occur in this prompt at all before v5.
        for field in ("event_types", "issue_codes", "affected_objects",
                      "impact_direction", "impact_magnitude", "production_mode",
                      "article_sentiment", "claim_type"):
            self.assertIn(field, ai_label.SYSTEM_PROMPT)


class JudgingSentencesThatCarryADecision(unittest.TestCase):
    """Named lines, so a regression says which judgement was lost."""

    def test_the_catch_all_type_is_marked_as_last_not_easiest(self):
        # 22 official event names fold into CUSTOMER_SUPPLIER_OPERATIONS
        # (event_taxonomy_map.yaml:163-183) while the issue-code side splits the
        # same names five ways.
        rules = pipeline.event_type_rules(4)
        self.assertIn("这是顺序上的最后一类，不是省事的一类。", rules)

    def test_the_speech_act_code_is_forbidden_as_a_fallback(self):
        # 174 of 225 stored RESPONSE_CLARIFICATION events are single-label, which
        # is what a fallback looks like when nothing declares it one.
        rules = pipeline.event_type_rules(4)
        self.assertIn("不得把它当兜底", rules)
        self.assertIn("同时给出", rules)

    def test_a_financial_number_is_not_a_financial_subject(self):
        # FINANCIAL_AUDIT: gold precision 0.033, predicted 90 against support 13.
        self.assertIn("文中出现财务数字不等于\n     本文在讲财务", pipeline.event_type_rules(4))

    def test_prospectus_data_does_not_make_a_story_about_the_offering(self):
        self.assertIn("数据是材料，主题是发行", pipeline.event_type_rules(4))

    def test_major_has_to_be_argued(self):
        # major sits on 36.3% of events, 566 of them positive+major: "important
        # sounding" rather than material.
        rules = pipeline.impact_rules()
        self.assertIn("major 是需要举证的判断", rules)
        self.assertIn("magnitude_basis", rules)

    def test_tone_and_impact_are_kept_on_separate_axes_in_both_directions(self):
        # The principle held since V1.0 (§8.4), and the pair of modules that own
        # the two axes each has to state it or they collapse back into one.
        self.assertIn("行文语气由 article_sentiment 另行承担", pipeline.impact_rules())
        self.assertIn("语气看措辞，不看事件后果", article_sentiment.SYSTEM_PROMPT)

    def test_the_background_word_defence_survives_in_both_copies(self):
        # event_emotion earned this line against pipeline's NEGATIVE_TERMS, which
        # contains 风险 and 调查 -- words that sit in unrelated background prose.
        for text in (pipeline.impact_rules(), event_emotion.SYSTEM_PROMPT):
            self.assertIn("不得仅凭出现这类词就判", text)

    def test_the_two_most_abused_impact_objects_are_named_as_such(self):
        # shareholders_control 50.7% of events, investors_stock_price 33.0%.
        rules = pipeline.impact_object_rules()
        self.assertIn("shareholders_control", rules)
        self.assertIn("investors_stock_price", rules)
        self.assertIn("任何披露都可能影响股价", rules)

    def test_both_anti_abuse_rules_are_mechanism_tests_not_topic_lists(self):
        """The difference between the two is measured, not stylistic.

        ``shareholders_control`` was written as a mechanism — 「只有股东自身的持股或
        控制关系被触动才给」 — and fell 50.7% -> 6.9% on the corpus.
        ``investors_stock_price`` was written as a topic list (「在讲股价、成交、估值
        或投资者权益时才给」) and rose 33.0% -> 56.8%, because this corpus's filings
        are dividends, conversions, placements and turnover flashes, all of which
        are on that list.  v2 rewrites the second one in the first one's shape.
        """
        rules = pipeline.impact_object_rules()
        self.assertIn("只有当**股东自身的\n  持股、控制关系或其权益**被触动时才给它", rules)
        self.assertIn("判据不是「稿件在讲\n  股价」，而是**这件事改变了每股的价值，"
                      "或改变了投资者可得的现金与股份**", rules)
        # And the exclusion that makes it bite: market pricing is not a change.
        self.assertIn("二级市场自身的交易结果不算", rules)

    def test_no_fit_is_an_empty_list_in_both_vocabularies(self):
        self.assertIn("返回空列表，不要硬凑", pipeline.event_type_rules(4))
        self.assertIn("返回空列表，不要硬凑", pipeline.issue_code_rules(3))


class OneCopyOfEveryRule(unittest.TestCase):
    """The extraction prompt and the sidecars must render the same text.

    ``max_types`` / ``max_codes`` is the only legitimate difference, exactly as in
    ``issue_code_rules``: the extraction schema allows four types, the narrow
    sidecar three.
    """

    def test_the_only_difference_between_the_two_type_ceilings_is_the_ceiling(self):
        four = pipeline.event_type_rules(4)
        three = pipeline.event_type_rules(3)
        self.assertIn("极少超过 3 个", four)
        self.assertIn("最多 3 个", three)
        self.assertEqual(four.split("\n- ", 1)[1], three.split("\n- ", 1)[1])

    def test_the_extraction_prompt_renders_the_shared_functions(self):
        self.assertIn(pipeline.event_type_rules(4), ai_label.SYSTEM_PROMPT)
        self.assertIn(pipeline.impact_rules(), ai_label.SYSTEM_PROMPT)
        self.assertIn(pipeline.issue_code_rules(3), ai_label.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
