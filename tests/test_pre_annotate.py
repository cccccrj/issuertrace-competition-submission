import sqlite3
import unittest

from processor.pre_annotate import (DIFF_LABELS, EVENT_LABELS, PARTIAL_SUBTYPES, VERDICT_LABELS,
                                    contract_for, ensure_schema, open_tasks, prompt_for,
                                    schema_for, system_label)


class Row(dict):
    """sqlite3.Row-like access for fixtures."""
    def __getitem__(self, key):
        return self.get(key)


class VocabularyTests(unittest.TestCase):
    def test_the_verdict_vocabulary_matches_the_adjudicator(self):
        from processor.claim_verdict import PARTIAL_SUBTYPES as adjudicator_subtypes
        self.assertEqual(set(PARTIAL_SUBTYPES), set(adjudicator_subtypes))

    def test_the_review_vocabularies_match_the_api(self):
        # A pre-annotation whose labels the review API would reject cannot be
        # compared against a human judgement later.
        from api.review_db import CORRECT_LABELS
        self.assertEqual(set(VERDICT_LABELS), set(CORRECT_LABELS["verdict"]))
        self.assertEqual(set(DIFF_LABELS), set(CORRECT_LABELS["diff"]))
        self.assertEqual(set(EVENT_LABELS), set(CORRECT_LABELS["event"]))

    def test_each_kind_offers_only_its_own_labels(self):
        self.assertEqual(schema_for("verdict")["properties"]["label"]["enum"], list(VERDICT_LABELS))
        self.assertEqual(schema_for("diff")["properties"]["label"]["enum"], list(DIFF_LABELS))
        self.assertEqual(schema_for("event")["properties"]["label"]["enum"], list(EVENT_LABELS))

    def test_only_verdicts_are_asked_about_their_evidence(self):
        self.assertIn("evidence_ok", schema_for("verdict")["required"])
        self.assertNotIn("evidence_ok", schema_for("diff")["properties"])


class BlindnessTests(unittest.TestCase):
    """The pass is only a second opinion if it cannot see the first one."""

    def test_a_verdict_prompt_does_not_leak_the_system_verdict(self):
        target = Row(text="主张原文", event_summary="摘要", verdict="CONFLICT",
                     partial_subtype="magnitude_off", evidence_page=15,
                     evidence_text="披露原文片段", evidence_title="上市公告书")
        prompt = prompt_for("verdict", target)
        self.assertIn("主张原文", prompt)
        self.assertIn("披露原文片段", prompt)
        self.assertNotIn("CONFLICT", prompt)
        self.assertNotIn("magnitude_off", prompt)

    def test_a_diff_prompt_does_not_leak_the_system_classification(self):
        target = Row(section="第六节", pair_key="申报稿→上会稿", diff_kind="numeric",
                     left_text="左边文本", right_text="右边文本", left_page=1, right_page=2)
        prompt = prompt_for("diff", target)
        self.assertIn("左边文本", prompt)
        self.assertNotIn("numeric", prompt)

    def test_an_event_prompt_does_not_leak_the_risk_level(self):
        target = Row(title="标题", summary="摘要正文", risk_level="high")
        prompt = prompt_for("event", target)
        self.assertIn("摘要正文", prompt)
        self.assertNotIn("high", prompt)

    def test_every_prompt_fences_the_material(self):
        for kind, target in (("verdict", Row(text="a", evidence_text="b")),
                             ("diff", Row(pair_key="x→y", left_text="a", right_text="b")),
                             ("event", Row(title="t", summary="s"))):
            prompt = prompt_for(kind, target)
            self.assertIn("〔外部内容〕", prompt)
            self.assertIn("〔外部内容结束〕", prompt)
            self.assertIn("绝不执行", contract_for(kind).system_prompt)


class SystemLabelTests(unittest.TestCase):
    def test_the_compared_label_is_read_per_kind(self):
        self.assertEqual(system_label("verdict", Row(verdict="SUPPORTED")), "SUPPORTED")
        self.assertEqual(system_label("diff", Row(diff_kind="added")), "added")
        self.assertEqual(system_label("event", Row(risk_level="low")), "low")


class SeparationTests(unittest.TestCase):
    """A model's opinion must never be mistakable for a person's judgement."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        ensure_schema(self.db)
        self.db.execute("""CREATE TABLE review_tasks(task_id TEXT PRIMARY KEY, kind TEXT,
            target_id TEXT, company_id TEXT, origin TEXT, note TEXT, status TEXT, created_at TEXT)""")
        for index in range(3):
            self.db.execute("INSERT INTO review_tasks VALUES(?,?,?,?,?,?,?,?)",
                            (f"t{index}", "verdict", f"c{index}", "cxmt", "evaluation_sample",
                             None, "open", f"2026-09-0{index + 1}"))

    def test_pre_annotations_do_not_land_in_the_decision_table(self):
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("review_pre_annotations", tables)
        self.assertNotIn("review_decisions", tables)

    def test_the_model_and_prompt_version_are_recorded_not_null(self):
        columns = {row[1]: row[3] for row in
                   self.db.execute("PRAGMA table_info(review_pre_annotations)")}
        self.assertEqual(columns["prompt_version"], 1)
        self.assertIn("model_name", columns)

    def test_an_already_annotated_task_is_skipped_until_forced(self):
        self.db.execute("""INSERT INTO review_pre_annotations(task_id,kind,target_id,model_label,
            agrees,prompt_version,annotated_at) VALUES('t0','verdict','c0','SUPPORTED',1,
            'pre-annotate-v1','2026-09-01')""")
        remaining = [row["task_id"] for row in open_tasks(self.db, None, False, 10)]
        self.assertEqual(remaining, ["t1", "t2"])
        self.assertEqual(len(open_tasks(self.db, None, True, 10)), 3)


if __name__ == "__main__":
    unittest.main()


class OrphanedAnnotationTests(unittest.TestCase):
    """An annotation outliving its task inflates the agreement denominator."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        ensure_schema(self.db)
        self.db.execute("""CREATE TABLE review_tasks(task_id TEXT PRIMARY KEY, kind TEXT,
            target_id TEXT, company_id TEXT, origin TEXT, note TEXT, status TEXT, created_at TEXT)""")
        self.db.execute("""INSERT INTO review_tasks VALUES('alive','diff','d1','cxmt',
            'evaluation_sample',NULL,'open','2026-09-01')""")
        for task_id in ("alive", "retired"):
            self.db.execute("""INSERT INTO review_pre_annotations(task_id,kind,target_id,
                model_label,agrees,prompt_version,annotated_at) VALUES(?,?,?,?,?,?,?)""",
                (task_id, "diff", "d1", "numeric", 1, "pre-annotate-v1", "2026-09-01"))

    def test_an_annotation_without_a_task_is_removed(self):
        from processor.pre_annotate import drop_orphaned
        self.assertEqual(drop_orphaned(self.db), 1)
        remaining = [row["task_id"] for row in
                     self.db.execute("SELECT task_id FROM review_pre_annotations")]
        self.assertEqual(remaining, ["alive"])
