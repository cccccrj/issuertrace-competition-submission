import tempfile
import unittest
from pathlib import Path
from unittest import mock

from processor import pipeline
from processor.benchmark import (
    DEFAULT_E5_PARAMS, b3_scores, connect, ensure_cluster_schema, ensure_schema,
    evaluate_e5, fbeta, gold_clusters, gold_documents, gold_version_id, pair_scores,
    predicted_clusters, render_report, representative_agreement, run_e5, sha256_text,
    sweep_e5, winning_params,
)


class PartitionMetricTests(unittest.TestCase):
    """Metrics on hand-built partitions, where the arithmetic can be checked."""

    GOLD = {"g1": {"P", "C1", "C2"}}
    UNIVERSE = {"P", "C1", "C2", "S"}

    def pairs(self, clusters):
        from processor.benchmark import _pairs
        return _pairs(clusters, self.UNIVERSE)

    def test_a_perfect_partition_scores_one(self):
        scores = pair_scores(self.pairs(self.GOLD), self.pairs({"p1": {"P", "C1", "C2"}}))
        self.assertEqual((scores["pair_precision"], scores["pair_recall"], scores["pair_f1"]),
                         (1.0, 1.0, 1.0))
        b3 = b3_scores(self.GOLD, {"p1": {"P", "C1", "C2"}}, self.UNIVERSE)
        self.assertEqual((b3["b3_precision"], b3["b3_recall"], b3["b3_f1"]), (1.0, 1.0, 1.0))

    def test_a_missed_member_costs_recall_only(self):
        predicted = {"p1": {"P", "C1"}}
        scores = pair_scores(self.pairs(self.GOLD), self.pairs(predicted))
        # Gold has 3 pairs, we found 1 of them and invented none.
        self.assertEqual(scores["pair_gold"], 3)
        self.assertEqual(scores["pair_predicted"], 1)
        self.assertEqual(scores["pair_precision"], 1.0)
        self.assertEqual(scores["pair_recall"], round(1 / 3, 4))
        self.assertEqual(scores["pair_f1"], 0.5)
        b3 = b3_scores(self.GOLD, predicted, self.UNIVERSE)
        self.assertEqual(b3["b3_precision"], 1.0)
        # (2/3 + 2/3 + 1/3 + 1) / 4
        self.assertEqual(b3["b3_recall"], round((2 / 3 + 2 / 3 + 1 / 3 + 1) / 4, 4))
        self.assertEqual(b3["b3_f1"], 0.8)

    def test_an_invented_pair_costs_precision_only(self):
        predicted = {"p1": {"P", "C1", "C2", "S"}}
        scores = pair_scores(self.pairs(self.GOLD), self.pairs(predicted))
        self.assertEqual(scores["pair_recall"], 1.0)
        self.assertEqual(scores["pair_predicted"], 6)
        self.assertEqual(scores["pair_precision"], 0.5)

    def test_b3_refuses_to_reward_one_giant_cluster(self):
        # Pair counting already dislikes this; B-cubed dislikes it more, which
        # is the reason both are reported.
        everything = {"p1": self.UNIVERSE}
        b3 = b3_scores(self.GOLD, everything, self.UNIVERSE)
        self.assertEqual(b3["b3_recall"], 1.0)
        self.assertLess(b3["b3_precision"], 0.85)

    def test_singletons_are_scored_not_ignored(self):
        # "S" is in neither gold cluster nor predicted cluster; getting it right
        # has to count for something or splitting everything would look perfect.
        b3 = b3_scores({}, {}, self.UNIVERSE)
        self.assertEqual((b3["b3_precision"], b3["b3_recall"]), (1.0, 1.0))

    def test_empty_inputs_do_not_divide_by_zero(self):
        self.assertEqual(pair_scores(set(), set())["pair_f1"], 0.0)
        self.assertEqual(b3_scores({}, {}, set())["b3_f1"], 0.0)


class GoldFixture(unittest.TestCase):
    """A hand-built gold database: one star, one singleton, one cross-split row."""

    def setUp(self):
        version = mock.patch("processor.benchmark.dataset_version", return_value="unit-e5-fixture")
        version.start()
        self.addCleanup(version.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = connect(Path(self.tmp.name) / "benchmark.sqlite3")
        self.addCleanup(self.db.close)
        ensure_schema(self.db)
        ensure_cluster_schema(self.db)
        body = "长鑫科技今日公布最新的存储芯片良率数据，产能爬坡进度符合预期。" * 8
        self.add("P", "train", "个股新闻", "co:000001", "良率数据公布", body, "2026-01-01")
        self.add("C1", "train", "个股重复新闻", None, "良率数据公布", body, "2026-01-02")
        self.add("C2", "train", "个股重复新闻", None, "良率数据公布", body, "2026-01-03")
        self.add("S", "train", "个股新闻", "co:000001", "完全不同的一则消息",
                 "公司召开股东大会并审议通过多项议案，与前述内容毫无关系。" * 8, "2026-01-04")
        self.add("SHORT", "train", "个股新闻", "co:000001", "太短", "短。", "2026-01-05")
        self.add("XSPLIT", "train", "个股重复新闻", None, "良率数据公布", body, "2026-01-06")
        self.add("PTEST", "test", "个股新闻", "co:000001", "良率数据公布", body, "2026-02-01")
        self.link("C1", "P", "train", "child_names_parent")
        self.link("C2", "P", "train", "child_names_parent")
        self.link("C1", "P", "train", "parent_lists_child")
        # XSPLIT's only parent is in the other split: it can inherit nothing
        # without leaking, so it must be excluded rather than guessed at.
        self.link("XSPLIT", "PTEST", "train", "child_names_parent")
        self.db.commit()

    def add(self, article_id, split, sheet, entity, title, text, published):
        self.db.execute("""INSERT INTO gold_articles
            (article_file_id,split,sheet,entity_kind,entity_id,article_source,title,
             published_at,content_hash,text_chars,version_id,imported_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (article_id, split, sheet, "company" if entity else None, entity,
             f"来源{article_id}", title, published, sha256_text(text), len(text),
             gold_version_id(article_id), "2026-09-02T00:00:00+00:00"))
        self.db.execute("INSERT INTO gold_article_text(article_file_id,text) VALUES(?,?)",
                        (article_id, text))

    def link(self, child, parent, split, direction):
        self.db.execute("""INSERT OR IGNORE INTO gold_duplicate_links
            (child_article_id,parent_article_id,split,link_direction) VALUES(?,?,?,?)""",
            (child, parent, split, direction))


class GoldShapingTests(GoldFixture):
    def test_duplicates_inherit_the_entity_of_their_parent(self):
        candidates, version_to_article, _ = gold_documents(self.db, "train")
        # C1 and C2 carry no entity of their own; without inheritance clustering
        # would drop them and the star could never be recovered.
        self.assertEqual({doc.company_id for doc, _ in candidates}, {"co:000001"})
        self.assertLessEqual({"C1", "C2"}, set(version_to_article.values()))

    def test_the_scored_set_is_exactly_what_clustering_will_see(self):
        candidates, version_to_article, excluded = gold_documents(self.db, "train")
        self.assertEqual(set(version_to_article.values()), {"P", "C1", "C2", "S"})
        self.assertEqual(excluded, {"SHORT": "text_below_cluster_floor",
                                    "XSPLIT": "no_entity_in_this_split"})

    def test_a_cross_split_parent_never_pulls_a_row_across(self):
        _, version_to_article, excluded = gold_documents(self.db, "train")
        self.assertNotIn("XSPLIT", set(version_to_article.values()))
        self.assertEqual(excluded["XSPLIT"], "no_entity_in_this_split")

    def test_gold_clusters_are_same_split_components(self):
        clusters = gold_clusters(self.db, "train")
        self.assertEqual([sorted(members) for members in clusters.values()],
                         [["C1", "C2", "P"]])

    def test_singletons_are_not_gold_clusters(self):
        for members in gold_clusters(self.db, "train").values():
            self.assertNotIn("S", members)


class EvaluationTests(GoldFixture):
    def test_identical_copies_are_recovered(self):
        scores, excluded = evaluate_e5(self.db, "train", DEFAULT_E5_PARAMS)
        self.assertEqual(scores["pair_gold"], 3)
        self.assertEqual(scores["pair_recall"], 1.0)
        self.assertEqual(scores["pair_precision"], 1.0)
        self.assertEqual(scores["scored_articles"], 4)

    def test_excluded_rows_are_named_not_absorbed_into_the_denominator(self):
        run_id, scores = run_e5(self.db, "train")
        self.assertEqual(scores["excluded_articles"], 2)
        self.assertEqual(scores["exclusion_reasons"],
                         {"text_below_cluster_floor": 1, "no_entity_in_this_split": 1})
        recorded = {row["article_file_id"]: row["reason"] for row in self.db.execute(
            "SELECT article_file_id, reason FROM benchmark_exclusions WHERE run_id=?", (run_id,))}
        self.assertEqual(recorded, {"SHORT": "text_below_cluster_floor",
                                    "XSPLIT": "no_entity_in_this_split"})

    def test_gold_pairs_lost_to_exclusion_are_reported(self):
        scores, _ = evaluate_e5(self.db, "train", DEFAULT_E5_PARAMS)
        # XSPLIT's pair with PTEST is not scorable within the train split.
        self.assertGreaterEqual(scores["gold_pairs_before_exclusions"], scores["pair_gold"])

    def test_a_stricter_bar_lowers_recall(self):
        loose, _ = evaluate_e5(self.db, "train", DEFAULT_E5_PARAMS)
        strict, _ = evaluate_e5(self.db, "train", {**DEFAULT_E5_PARAMS, "hamming_max": 0,
                                                   "title_threshold": 0.999,
                                                   "body_threshold": 0.999})
        self.assertLessEqual(strict["pair_recall"], loose["pair_recall"])

    def test_a_run_records_its_parameters_and_completion(self):
        run_id, _ = run_e5(self.db, "train")
        row = self.db.execute("SELECT * FROM benchmark_runs WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["split"], "train")
        self.assertIn("title_threshold", row["params_json"])
        self.assertIsNotNone(row["completed_at"])

    def test_the_harness_writes_no_corpus_table(self):
        run_e5(self.db, "train")
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("event_records", tables)
        self.assertNotIn("event_claims", tables)


class SweepTests(GoldFixture):
    def test_sweeping_the_test_split_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            sweep_e5(self.db, "test", {"hamming_max": [4, 8]})
        self.assertIn("train", str(caught.exception))

    def test_a_sweep_records_every_grid_point_and_a_winner(self):
        run_id, results = sweep_e5(self.db, "train",
                                   {"hamming_max": [4, 8], "title_threshold": [0.6, 0.72]})
        self.assertEqual(len(results), 4)
        row = self.db.execute("""SELECT detail_json FROM benchmark_metrics
            WHERE run_id=? AND metric='sweep'""", (run_id,)).fetchone()
        import json
        detail = json.loads(row["detail_json"])
        self.assertEqual(len(detail["results"]), 4)
        self.assertIn("best_by_pair_f1", detail)
        self.assertEqual(detail["shipped"], DEFAULT_E5_PARAMS)


class SweepReportTests(GoldFixture):
    def test_the_table_names_the_gates_this_sweep_varied(self):
        # The header used to be four hard-coded columns, so a sweep over any
        # other gate rendered a row of empty cells -- a calibration record that
        # records nothing.  The header is now read off the results.
        run_id, _ = sweep_e5(self.db, "train", {"containment_threshold": [0.7, 0.8]})
        report = render_report(self.db, run_id=run_id, task="e5-sweep", fmt="md")
        header = next(line for line in report.splitlines() if line.startswith("| "))
        self.assertIn("containment_threshold", header)
        self.assertNotIn("hamming", header)


class ClusteringDefaultsTests(unittest.TestCase):
    def test_the_shipped_defaults_are_the_module_constants(self):
        # If someone edits one and not the other, the calibration record starts
        # describing parameters the pipeline is not using.
        self.assertEqual(DEFAULT_E5_PARAMS, {
            "title_threshold": pipeline.TITLE_MATCH_THRESHOLD,
            "body_threshold": pipeline.BODY_ONLY_THRESHOLD,
            "hamming_max": pipeline.SIMHASH_HAMMING_MAX,
            "bands": pipeline.SIMHASH_BANDS,
            "containment_threshold": pipeline.TITLE_CONTAINMENT_THRESHOLD,
            "max_reprint_gap_days": pipeline.REPRINT_GAP_DAYS,
            "template_text_bar": pipeline.TEMPLATE_TEXT_BAR,
            "template_number_bar": pipeline.TEMPLATE_NUMBER_BAR,
            "template_number_share": pipeline.TEMPLATE_NUMBER_SHARE,
        })

    def test_the_constants_hold_the_calibrated_values(self):
        # Changed 2026-09-02 from 8/4 on the strength of the duplication gold;
        # the record is docs/submission/04-模型与算法.md.  Pinned so that a later
        # edit has to come with a reason and an updated calibration record.
        self.assertEqual(pipeline.TITLE_MATCH_THRESHOLD, 0.72)
        self.assertEqual(pipeline.BODY_ONLY_THRESHOLD, 0.86)
        self.assertEqual(pipeline.SIMHASH_HAMMING_MAX, 16)
        self.assertEqual(pipeline.SIMHASH_BANDS, 8)
        # Added 2026-09-05.  The bar is 0.80 although the train sweep's F1 peak
        # is 0.50: the 0.50-0.80 band on the platform's own corpus is where
        # 巨潮 files FF301, FF304 and FF305 confuse each other, and the gold --
        # which is news, not filings -- cannot see that.  同 §交付物7.
        self.assertEqual(pipeline.TITLE_CONTAINMENT_THRESHOLD, 0.80)
        self.assertEqual(pipeline.REPRINT_GAP_DAYS, 7)
        # Added and rejected on 2026-09-05 (交付物7 §7ter).  The bar is None, and
        # that is the result: seventeen grid points, none of them better than
        # leaving the veto off.  The other two keep the values the sweep would
        # have chosen, so re-enabling it is a one-line change with a record.
        self.assertIsNone(pipeline.TEMPLATE_TEXT_BAR)
        self.assertEqual(pipeline.TEMPLATE_NUMBER_BAR, 0.50)
        self.assertEqual(pipeline.TEMPLATE_NUMBER_SHARE, 0.15)

    def test_the_band_count_divides_the_signature_evenly(self):
        # band_width = 64 // bands; a count that does not divide 64 would drop
        # signature bits silently.
        self.assertEqual(64 % pipeline.SIMHASH_BANDS, 0)


if __name__ == "__main__":
    unittest.main()


class FrozenParameterTests(GoldFixture):
    """The test split is spent once, on parameters chosen from the train split."""

    def test_winning_params_come_back_from_a_finished_sweep(self):
        run_id, _ = sweep_e5(self.db, "train",
                             {"hamming_max": [4, 8], "title_threshold": [0.6, 0.72]})
        params = winning_params(self.db, run_id)
        self.assertEqual(set(params), {"hamming_max", "title_threshold"})
        self.assertIn(params["hamming_max"], (4, 8))

    def test_a_run_id_that_is_not_a_sweep_is_refused(self):
        run_id, _ = run_e5(self.db, "train")
        with self.assertRaises(KeyError):
            winning_params(self.db, run_id)

    def test_an_unknown_run_id_is_refused(self):
        with self.assertRaises(KeyError):
            winning_params(self.db, "0000000000000000")

    def test_frozen_parameters_reproduce_the_score_they_were_chosen_for(self):
        # The point of --params-from: the test run must use exactly the numbers
        # the sweep settled on, not today's defaults.
        sweep_id, results = sweep_e5(self.db, "train",
                                     {"title_threshold": [0.6, 0.72]})
        params = winning_params(self.db, sweep_id)
        best = max(results, key=lambda item: item["pair_f1"])
        scores, _ = evaluate_e5(self.db, "train", {**DEFAULT_E5_PARAMS, **params})
        self.assertEqual(scores["pair_f1"], best["pair_f1"])

    def test_the_sweep_records_how_far_it_got(self):
        # Checkpointed per grid point so a long run is observable and a crash
        # does not discard the hour before it.
        import json
        run_id, _ = sweep_e5(self.db, "train", {"title_threshold": [0.6, 0.72]})
        detail = json.loads(self.db.execute(
            "SELECT detail_json FROM benchmark_metrics WHERE run_id=? AND metric='sweep'",
            (run_id,)).fetchone()["detail_json"])
        self.assertEqual(detail["completed"], 2)
        self.assertEqual(detail["of"], 2)


class FBetaTests(unittest.TestCase):
    """Recall is worth more than precision for this particular gate.

    A missed duplicate leaves two copies of one story counting as two
    independent sources, which inflates corroboration -- the failure the design
    exists to prevent.  A false merge deflates it instead.  Both are errors; only
    one of them overstates the evidence, so F2 is reported beside F1.
    """

    def test_f2_equals_f1_when_precision_equals_recall(self):
        self.assertEqual(fbeta(0.9, 0.9, 2), fbeta(0.9, 0.9, 1))

    def test_f2_punishes_the_precision_heavy_corner(self):
        # The shipped gate's own shape: near-perfect precision, 21% of true
        # duplicate pairs missed.
        self.assertLess(fbeta(0.9944, 0.7875, 2), fbeta(0.9944, 0.7875, 1))

    def test_f2_rewards_the_recall_heavy_corner(self):
        self.assertGreater(fbeta(0.7875, 0.9944, 2), fbeta(0.7875, 0.9944, 1))

    def test_f1_is_symmetric_but_f2_is_not(self):
        self.assertEqual(fbeta(0.6, 0.9, 1), fbeta(0.9, 0.6, 1))
        self.assertNotEqual(fbeta(0.6, 0.9, 2), fbeta(0.9, 0.6, 2))

    def test_a_zero_component_scores_zero_rather_than_dividing_by_zero(self):
        self.assertEqual(fbeta(0.0, 1.0, 2), 0.0)
        self.assertEqual(fbeta(1.0, 0.0, 2), 0.0)
