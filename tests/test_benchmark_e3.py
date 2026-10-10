import tempfile
import unittest
from pathlib import Path
from unittest import mock

from processor.benchmark import (
    EMOTION_OF_DIRECTION, GOLD_EMOTIONS, SENTIMENT_FIELDS, connect, ensure_schema,
    evaluate_e3, gold_version_id, labelled_documents, majority_baseline, run_e3,
    sha256_text,
)


class MajorityBaselineTests(unittest.TestCase):
    def test_the_baseline_is_the_commonest_class(self):
        result = majority_baseline(["正面"] * 9 + ["负面"])
        self.assertEqual(result["label"], "正面")
        self.assertEqual(result["accuracy"], 0.9)
        self.assertEqual(result["support"], 10)

    def test_a_skewed_gold_gives_a_high_accuracy_and_a_poor_macro_f1(self):
        # The whole reason the baseline is reported: 90% accuracy here means
        # nothing, and macro-F1 is what says so.
        result = majority_baseline(["正面"] * 9 + ["负面"])
        self.assertGreater(result["accuracy"], 0.85)
        self.assertLess(result["macro_f1"], 0.5)

    def test_an_empty_gold_returns_nothing_rather_than_zero(self):
        self.assertEqual(majority_baseline([]),
                         {"label": None, "accuracy": None, "macro_f1": None})


class E3Fixture(unittest.TestCase):
    def setUp(self):
        # This fixture tests synthetic rows, not the organiser's private Excel files.
        version = mock.patch("processor.benchmark.dataset_version", return_value="unit-e3-fixture")
        version.start()
        self.addCleanup(version.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = connect(Path(self.tmp.name) / "benchmark.sqlite3")
        self.addCleanup(self.db.close)
        ensure_schema(self.db)
        self.add("G1", "个股新闻", "co:000001", "长鑫科技良率提升并完成新产品交付",
                 "公司宣布产品良率提升，技术突破带动业绩增长与新产能交付。" * 6, "正面")
        self.add("G2", "个股新闻", "co:000001", "公司遭监管处罚并面临诉讼",
                 "监管部门对公司作出行政处罚，同时公司面临诉讼与业绩下滑风险。" * 6, "负面")
        self.add("G3", "行业新闻", "ind:110100", "行业政策发布",
                 "监管部门发布行业政策，对相关企业的经营与技术投入提出要求。" * 6, "中性")
        self.add("BAD", "个股新闻", "co:000001", "标注缺失的一行",
                 "公司发布了一则与技术产品有关的普通公告。" * 6, "未知")
        self.db.commit()

    def add(self, article_id, sheet, entity, title, text, emotion):
        self.db.execute("""INSERT INTO gold_articles
            (article_file_id,split,sheet,entity_kind,entity_id,article_source,title,
             published_at,content_hash,text_chars,version_id,imported_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (article_id, "train", sheet, "company", entity, "新浪财经", title,
             "2026-01-01", sha256_text(text), len(text), gold_version_id(article_id),
             "2026-09-02T00:00:00+00:00"))
        self.db.execute("INSERT INTO gold_article_text(article_file_id,text) VALUES(?,?)",
                        (article_id, text))
        self.db.execute("""INSERT INTO gold_event_labels
            (article_file_id,event_name,event_emotion,impact_analysis_hash)
            VALUES(?,?,?,?)""", (article_id, "产品技术创新", emotion, None))


class LabelledDocumentTests(E3Fixture):
    def test_only_labelled_sheets_are_returned(self):
        ids = {row["article_file_id"] for _, _, row in labelled_documents(self.db, "train")}
        self.assertEqual(ids, {"G1", "G2", "G3", "BAD"})

    def test_documents_carry_the_entity_as_company_id(self):
        docs = {row["article_file_id"]: doc for doc, _, row in labelled_documents(self.db, "train")}
        self.assertEqual(docs["G1"].company_id, "co:000001")
        self.assertEqual(docs["G3"].company_id, "ind:110100")

    def test_limit_caps_the_selection(self):
        self.assertEqual(len(labelled_documents(self.db, "train", limit=2)), 2)


class E3EvaluationTests(E3Fixture):
    def test_a_gold_label_outside_the_vocabulary_is_excluded_not_guessed(self):
        scores, excluded = evaluate_e3(self.db, "train")
        self.assertEqual(excluded["BAD"], "gold_emotion_outside_vocabulary")
        self.assertEqual(scores["scored_articles"], 3)

    def test_both_polarity_fields_are_scored(self):
        scores, _ = evaluate_e3(self.db, "train")
        for field in SENTIMENT_FIELDS:
            self.assertIn("macro_f1", scores[field])
            self.assertEqual(scores[field]["sample_size"], 3)

    def test_the_two_fields_are_identical_on_the_rule_path(self):
        # article_sentiment is assigned from impact_direction in pipeline.rule_label,
        # so a rate below 1.0 would mean that assignment had changed.
        scores, _ = evaluate_e3(self.db, "train")
        self.assertEqual(scores["fields_agree"]["rate"], 1.0)

    def test_the_baseline_is_reported_beside_the_scores(self):
        scores, _ = evaluate_e3(self.db, "train")
        self.assertIn("majority_baseline", scores)
        self.assertEqual(scores["gold_distribution"], {"正面": 1, "负面": 1, "中性": 1})

    def test_predictions_use_the_gold_vocabulary(self):
        scores, _ = evaluate_e3(self.db, "train")
        for values in scores["predictions"].values():
            for field in SENTIMENT_FIELDS:
                self.assertIn(values[field], GOLD_EMOTIONS)

    def test_a_negative_article_is_read_as_negative(self):
        scores, _ = evaluate_e3(self.db, "train")
        self.assertEqual(scores["predictions"]["G2"]["impact_direction"], "负面")


class E3RunTests(E3Fixture):
    def test_a_run_stores_predictions_exclusions_and_metrics(self):
        run_id, scores = run_e3(self.db, "train")
        stored = self.db.execute(
            "SELECT COUNT(*) FROM benchmark_predictions WHERE run_id=? AND task='e3-rule'",
            (run_id,)).fetchone()[0]
        self.assertEqual(stored, scores["scored_articles"])
        self.assertEqual(self.db.execute(
            "SELECT reason FROM benchmark_exclusions WHERE run_id=? AND article_file_id='BAD'",
            (run_id,)).fetchone()["reason"], "gold_emotion_outside_vocabulary")
        metrics = {row["metric"] for row in self.db.execute(
            "SELECT metric FROM benchmark_metrics WHERE run_id=?", (run_id,))}
        self.assertLessEqual({"impact_direction", "article_sentiment", "majority_baseline"},
                             metrics)

    def test_a_capped_run_records_its_cap(self):
        run_id, _ = run_e3(self.db, "train", 2)
        self.assertEqual(self.db.execute(
            "SELECT sample_cap FROM benchmark_runs WHERE run_id=?", (run_id,)).fetchone()[0], 2)

    def test_the_run_writes_no_corpus_table(self):
        run_e3(self.db, "train")
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("event_records", tables)


class VocabularyTests(unittest.TestCase):
    def test_the_direction_mapping_covers_the_whole_gold_vocabulary(self):
        self.assertEqual(set(EMOTION_OF_DIRECTION.values()), set(GOLD_EMOTIONS))


if __name__ == "__main__":
    unittest.main()
