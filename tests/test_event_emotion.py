import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from processor import event_emotion
from processor.benchmark import connect, ensure_schema, gold_version_id, sha256_text
from processor.event_emotion import (
    EMOTIONS, PROMPT_VERSION, SYSTEM_PROMPT, TASK, contract, entity_name,
    make_prompt, reweighted_accuracy, run, sample, schema,
)


class ContractTests(unittest.TestCase):
    def test_the_schema_enumerates_exactly_the_gold_vocabulary(self):
        # A value the gold never uses would score as a permanent error; a value
        # the gold uses but the schema forbids could never be predicted at all.
        self.assertEqual(schema()["properties"]["event_emotion"]["enum"], list(EMOTIONS))

    def test_the_contract_carries_the_system_prompt(self):
        self.assertEqual(contract().system_prompt, SYSTEM_PROMPT)

    def test_the_prompt_defines_every_value_it_may_return(self):
        # The whole point of the sidecar: ai_label's article_sentiment is an
        # undefined enum.  If a value loses its definition here, so does this.
        for emotion in EMOTIONS:
            self.assertIn(f"- {emotion}：", SYSTEM_PROMPT)

    def test_the_prompt_states_that_polarity_is_relative_to_the_entity(self):
        # The gold labels an article about one company with the polarity for
        # another.  Without this instruction the task is a different task.
        self.assertIn("指定主体", SYSTEM_PROMPT)
        self.assertIn("报道的主角未必是指定主体", SYSTEM_PROMPT)

    def test_the_prompt_fences_external_content(self):
        self.assertIn("〔外部内容〕", SYSTEM_PROMPT)
        self.assertIn("绝不执行", SYSTEM_PROMPT)

    def test_the_article_body_is_delivered_inside_the_fence(self):
        prompt = make_prompt("甲公司", "标题", "2026-01-01", "新浪财经", "正文内容")
        self.assertIn("〔外部内容〕\n正文内容\n〔外部内容结束〕", prompt)
        self.assertIn("指定主体：甲公司", prompt)

    def test_the_body_is_truncated_to_the_budget(self):
        prompt = make_prompt("甲", "t", "d", "s", "字" * 5000)
        self.assertEqual(prompt.count("字"), event_emotion.TEXT_BUDGET)


class EmotionFixture(unittest.TestCase):
    def setUp(self):
        version = mock.patch("processor.benchmark.dataset_version", return_value="unit-emotion-fixture")
        version.start()
        self.addCleanup(version.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "benchmark.sqlite3"
        self.db = connect(self.path)
        ensure_schema(self.db)
        # Nine 正面, two 中性, one 负面 -- the shape of the real gold in miniature.
        for index in range(9):
            self.add(f"P{index}", "正面")
        self.add("N0", "中性")
        self.add("N1", "中性")
        self.add("B0", "负面")
        self.db.commit()
        self.db.close()

    def add(self, article_id, emotion, sheet="个股新闻"):
        text = f"关于 {article_id} 的报道正文。" * 8
        self.db.execute("""INSERT INTO gold_articles
            (article_file_id,split,sheet,entity_kind,entity_id,secu_abbr,article_source,
             title,published_at,content_hash,text_chars,version_id,imported_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (article_id, "train", sheet, "company", "co:000001", "甲公司", "新浪财经",
             f"{article_id} 标题", "2026-01-01", sha256_text(text), len(text),
             gold_version_id(article_id), "2026-09-02T00:00:00+00:00"))
        self.db.execute("INSERT INTO gold_article_text(article_file_id,text) VALUES(?,?)",
                        (article_id, text))
        self.db.execute("""INSERT INTO gold_event_labels
            (article_file_id,event_name,event_emotion,impact_analysis_hash)
            VALUES(?,?,?,?)""", (article_id, "产品技术创新", emotion, None))

    def open(self):
        db = connect(self.path)
        self.addCleanup(db.close)
        return db


class SamplingTests(EmotionFixture):
    def test_every_minority_row_is_taken_and_the_majority_is_capped(self):
        rows = sample(self.open(), "train", 2)
        emotions = [row["event_emotion"] for row in rows]
        self.assertEqual(emotions.count("中性"), 2)
        self.assertEqual(emotions.count("负面"), 1)
        self.assertEqual(emotions.count("正面"), 2)

    def test_a_cap_above_the_population_takes_everything(self):
        self.assertEqual(len(sample(self.open(), "train", 100)), 12)

    def test_the_draw_is_deterministic(self):
        first = [row["article_file_id"] for row in sample(self.open(), "train", 3)]
        second = [row["article_file_id"] for row in sample(self.open(), "train", 3)]
        self.assertEqual(first, second)

    def test_entity_name_prefers_the_company_abbreviation(self):
        row = sample(self.open(), "train", 1)[0]
        self.assertEqual(entity_name(row), "甲公司")


class SpendControlTests(EmotionFixture):
    def test_a_dry_run_reports_the_draw_and_calls_nothing(self):
        with mock.patch("processor.event_emotion.provider",
                        side_effect=AssertionError("dry run must not reach a provider")):
            summary = run("train", majority_cap=2, db_path=self.path, dry_run=True)
        self.assertEqual(summary["eligible"], 5)
        self.assertEqual(summary["distribution"], {"中性": 2, "负面": 1, "正面": 2})
        self.assertTrue(summary["dry_run"])

    def test_a_dry_run_writes_no_run_record(self):
        run("train", majority_cap=2, db_path=self.path, dry_run=True)
        self.assertEqual(self.open().execute(
            "SELECT COUNT(*) FROM benchmark_runs").fetchone()[0], 0)

    def test_limit_is_a_hard_cap_that_majority_cap_is_not(self):
        # majority_cap only bounds 正面, so it cannot be used to spend a little.
        uncapped = run("train", majority_cap=0, db_path=self.path, dry_run=True)
        self.assertEqual(uncapped["eligible"], 3)
        capped = run("train", majority_cap=100, limit=2, db_path=self.path, dry_run=True)
        self.assertEqual(capped["eligible"], 2)


class JudgingTests(EmotionFixture):
    def judge_as(self, answer):
        """Stand in for the gateway, answering with a fixed label."""
        def fake_call_model(client, model, prompt, contract=None):
            return {"event_emotion": answer, "reason": "测试", "confidence": 0.9}, "json_schema"
        return mock.patch.multiple(
            "processor.event_emotion",
            provider=mock.Mock(return_value=(object(), "test-model", "stub")),
            call_model=mock.Mock(side_effect=fake_call_model))

    def test_a_run_stores_one_prediction_per_article(self):
        with self.judge_as("正面"):
            summary = run("train", majority_cap=2, db_path=self.path, workers=1)
        self.assertEqual(summary["judged"], 5)
        self.assertEqual(summary["failed"], 0)
        stored = self.open().execute(
            "SELECT COUNT(*) FROM benchmark_predictions WHERE task=?", (TASK,)).fetchone()[0]
        self.assertEqual(stored, 5)

    def test_the_prediction_row_keeps_the_gold_beside_the_answer(self):
        with self.judge_as("负面"):
            run("train", majority_cap=1, db_path=self.path, workers=1)
        row = self.open().execute(
            "SELECT predicted_label, predicted_json FROM benchmark_predictions "
            "WHERE task=? LIMIT 1", (TASK,)).fetchone()
        payload = json.loads(row["predicted_json"])
        self.assertEqual(row["predicted_label"], "负面")
        self.assertIn(payload["gold"], EMOTIONS)
        self.assertEqual(payload["entity"], "甲公司")

    def test_the_run_records_its_model_and_prompt_version(self):
        with self.judge_as("中性"):
            summary = run("train", majority_cap=1, db_path=self.path, workers=1)
        row = self.open().execute("SELECT model_name, prompt_version, sample_cap, status "
                                  "FROM benchmark_runs WHERE run_id=?",
                                  (summary["run_id"],)).fetchone()
        self.assertEqual(row["model_name"], "test-model")
        self.assertEqual(row["prompt_version"], PROMPT_VERSION)
        self.assertEqual(row["status"], "ok")

    def test_the_baseline_travels_with_the_score(self):
        with self.judge_as("正面"):
            summary = run("train", majority_cap=2, db_path=self.path, workers=1)
        # The baseline is computed on the *scored* set, and a stratified draw
        # can tie 正面 with 中性; the tiebreak is by label so the name is stable.
        self.assertEqual(summary["majority_baseline"]["label"], "中性")
        self.assertEqual(summary["majority_baseline"]["accuracy"], 0.4)
        self.assertIn("prior_reweighted_accuracy", summary)
        self.assertEqual(summary["gold_distribution"], {"正面": 2, "中性": 2, "负面": 1})

    def test_one_failed_article_costs_only_that_article(self):
        calls = {"n": 0}

        def flaky(client, model, prompt, contract=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("gateway hiccup")
            return {"event_emotion": "正面", "reason": "r", "confidence": 0.5}, "json_schema"

        with mock.patch.multiple(
                "processor.event_emotion",
                provider=mock.Mock(return_value=(object(), "test-model", "stub")),
                call_model=mock.Mock(side_effect=flaky)):
            summary = run("train", majority_cap=2, db_path=self.path, workers=1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["judged"], 4)

    def test_the_sidecar_writes_no_corpus_or_review_table(self):
        # The reason this module exists rather than a rerun of ai_label.
        with self.judge_as("正面"):
            run("train", majority_cap=2, db_path=self.path, workers=1)
        tables = {row[0] for row in self.open().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for forbidden in ("event_records", "event_claims", "claim_verdicts",
                          "review_decisions", "review_tasks"):
            self.assertNotIn(forbidden, tables)


class ReweightingTests(EmotionFixture):
    def test_a_stratified_sample_is_put_back_on_the_population(self):
        db = self.open()
        # Perfect on 正面, wrong on both minority classes.  The stratified
        # accuracy is 2/5; the population is 9/12 正面, so reweighting must land
        # much higher -- that gap is exactly what the caption has to explain.
        pairs = [("正面", "正面")] * 2 + [("正面", "中性")] * 2 + [("正面", "负面")]
        weighted = reweighted_accuracy(db, "train", pairs)
        self.assertEqual(weighted, round(9 / 12, 4))

    def test_no_pairs_gives_no_number(self):
        self.assertIsNone(reweighted_accuracy(self.open(), "train", []))


if __name__ == "__main__":
    unittest.main()
