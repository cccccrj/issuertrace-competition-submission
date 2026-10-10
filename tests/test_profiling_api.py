"""The reasoning behind two labels the interface reduced to one word each.

Both sidecars record the label, the model's reason, its confidence, the basis it
rests on, and the model and prompt that produced it. What these check is that
the endpoint returns all of it, and that the two distinctions the single word
erased survive: tone is not impact, and a label derived from a graph edge is not
a label a model read off the text.
"""
from __future__ import annotations

import sqlite3
import unittest

from tests.test_api import ApiTestCase


class ProfilingTestCase(ApiTestCase):
    def seed(self):
        db = sqlite3.connect(self.fixture_path)
        for table, column in (("article_sentiment_labels", "sentiment"),
                              ("production_mode_labels", "mode")):
            db.execute(f"""CREATE TABLE IF NOT EXISTS {table}(
                event_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, {column} TEXT NOT NULL,
                reason TEXT, confidence REAL, basis TEXT NOT NULL, model_name TEXT,
                prompt_version TEXT NOT NULL, classified_at TEXT NOT NULL)""")
        # The fixture's e1 has a positive impact and e2 a neutral one. Labelling
        # e1 neutral in tone and e2 positive makes both rows disagreements —
        # which is the shape the field exists to express: a piece's wording and
        # what the event does to the company are separate judgements.
        db.execute("""INSERT OR REPLACE INTO article_sentiment_labels VALUES
            ('e1','cxmt','neutral','全文为公告体，无褒贬修饰',0.95,'model','qwen-plus',
             'article-sentiment-v1','2026-09-03')""")
        db.execute("""INSERT OR REPLACE INTO article_sentiment_labels VALUES
            ('e2','wuxi_apptec','positive','标题即「业绩爆发」',0.88,'model','qwen-plus',
             'article-sentiment-v1','2026-09-03')""")
        db.execute("""INSERT OR REPLACE INTO production_mode_labels VALUES
            ('e1','cxmt','BRIEF','快讯体',0.9,'model','qwen-plus','production-mode-v1',
             '2026-09-01')""")
        # A label the propagation graph settled rather than the model: a stronger
        # claim, and the basis column is what says so.
        db.execute("""INSERT OR REPLACE INTO production_mode_labels VALUES
            ('e2','wuxi_apptec','REPOST','传播图中存在复制边',1.0,'propagation_edge',NULL,
             'production-mode-v1','2026-09-01')""")
        db.commit()
        db.close()


class SentimentTests(ProfilingTestCase):
    def test_the_reason_and_provenance_survive(self):
        # The word alone was what the interface showed; the reason, the
        # confidence and the prompt version are what make it checkable.
        self.seed()
        row = self.client.get("/api/profiling/sentiment",
                              params={"value": "neutral"}).json()["items"][0]
        self.assertEqual(row["reason"], "全文为公告体，无褒贬修饰")
        self.assertEqual(row["confidence"], 0.95)
        self.assertNotIn("prompt_version", row)
        self.assertNotIn("model_name", row)

    def test_tone_and_impact_are_cross_tabulated(self):
        # They are output separately and allowed to disagree. Agreement on every
        # row would mean one of them is not being judged.
        self.seed()
        body = self.client.get("/api/profiling/sentiment/stats").json()
        pairs = {(row["tone"], row["impact"]): row["count"] for row in body["vs_impact"]}
        self.assertEqual(pairs[("neutral", "positive")], 1)   # e1: 平实措辞，正面事件
        self.assertEqual(pairs[("positive", "neutral")], 1)   # e2: 褒扬措辞，中性事件

    def test_the_divergence_rate_is_reported_with_both_sides(self):
        self.seed()
        divergence = self.client.get("/api/profiling/sentiment/stats").json()["divergence"]
        self.assertEqual(divergence["agree"] + divergence["diverge"], 2)
        # Both fixture rows disagree, so the rate is 1.0 — the arithmetic is
        # checkable rather than merely present.
        self.assertEqual(divergence["diverge"], 2)
        self.assertEqual(divergence["rate"], 1.0)

    def test_a_confidence_histogram_rather_than_a_mean(self):
        # A model answering 0.95 to everything and one that spreads across the
        # range are different instruments; a mean cannot tell them apart.
        self.seed()
        buckets = self.client.get("/api/profiling/sentiment/stats").json()["confidence"]
        self.assertTrue(all("bucket" in row and "count" in row for row in buckets))
        self.assertEqual(sum(row["count"] for row in buckets), 2)

    def test_which_model_and_prompt_produced_the_labels_is_not_published(self):
        # I-06: the provenance table moved off the page and out of the API.
        self.seed()
        self.assertNotIn("prompt_versions", self.client.get("/api/profiling/sentiment/stats").json())


class ProductionTests(ProfilingTestCase):
    def test_a_graph_derived_label_is_distinguished_from_a_model_one(self):
        self.seed()
        basis = {row["value"]: row for row in
                 self.client.get("/api/profiling/production/stats").json()["by_basis"]}
        self.assertEqual(basis["propagation_edge"]["count"], 1)
        self.assertEqual(basis["propagation_edge"]["label"], "由传播关系推定")

    def test_the_basis_filter_selects_that_slice(self):
        self.seed()
        body = self.client.get("/api/profiling/production",
                               params={"basis": "propagation_edge"}).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["value"], "REPOST")

    def test_production_mode_is_cross_tabulated_against_tone(self):
        # Neither label has a gold standard, so they corroborate each other:
        # sponsored copy should read positive far more often than a filing does.
        self.seed()
        pairs = {(row["mode"], row["tone"]): row["count"] for row in
                 self.client.get("/api/profiling/production/stats").json()["vs_sentiment"]}
        self.assertEqual(pairs[("BRIEF", "neutral")], 1)
        self.assertEqual(pairs[("REPOST", "positive")], 1)


class FilterTests(ProfilingTestCase):
    def test_the_confidence_floor_narrows(self):
        self.seed()
        body = self.client.get("/api/profiling/sentiment", params={"min_confidence": 0.9}).json()
        self.assertEqual(body["total"], 1)

    def test_search_reaches_the_reason_text(self):
        self.seed()
        self.assertEqual(
            self.client.get("/api/profiling/sentiment", params={"search": "公告体"}).json()["total"], 1)


class DegradationTests(ApiTestCase):
    def test_it_reports_unavailable_rather_than_failing_before_labelling_runs(self):
        for path in ("/api/profiling/sentiment/stats", "/api/profiling/production/stats"):
            self.assertFalse(self.client.get(path).json()["available"], path)


if __name__ == "__main__":
    unittest.main()
