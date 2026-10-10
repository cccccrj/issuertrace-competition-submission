"""Near-duplicate clustering over the API — 命题主要任务第 4 项.

The clustering has been running since the beginning and was visible only as a
member count on a propagation row. These check the endpoint that exposes it, and
in particular the one distinction the page exists to draw: a near-duplicate
cluster (grouped by text similarity) is not a propagation component (grouped by
copy relations), and an independent-source count that conflates them is inflated.

Tables and rows are created inside the tests rather than in the shared fixture:
this data has no bearing on the overview counts, the vocabulary tallies or the
review sampling, and adding it there would move assertions in five other classes
that are about none of it.
"""
from __future__ import annotations

import sqlite3
import unittest

from tests.test_api import ApiTestCase


class DuplicateTestCase(ApiTestCase):
    def seed(self):
        db = sqlite3.connect(self.fixture_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS duplicate_clusters(
            cluster_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, cluster_kind TEXT NOT NULL,
            representative_version_id TEXT NOT NULL, member_count INTEGER NOT NULL,
            similarity_threshold REAL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS duplicate_cluster_members(
            cluster_id TEXT NOT NULL, version_id TEXT NOT NULL, PRIMARY KEY(cluster_id, version_id));
        CREATE TABLE IF NOT EXISTS propagation_component_members(
            component_id TEXT NOT NULL, version_id TEXT NOT NULL, depth INTEGER);
        CREATE TABLE IF NOT EXISTS propagation_components(
            component_id TEXT PRIMARY KEY, company_id TEXT, member_count INTEGER,
            source_count INTEGER, first_published_at TEXT, last_published_at TEXT,
            root_version_id TEXT);
        """)
        # Two documents from two different outlets carrying the same text: one
        # cluster, two independent sources.
        db.execute("""INSERT OR REPLACE INTO document_versions
            (version_id, source_id, company_id, title, published_at)
            VALUES('v-a','media_a','cxmt','长鑫科技募资 295 亿元','2026-08-01')""")
        db.execute("""INSERT OR REPLACE INTO document_versions
            (version_id, source_id, company_id, title, published_at)
            VALUES('v-b','media_b','cxmt','长鑫科技募资 295 亿元','2026-08-01')""")
        db.execute("""INSERT INTO duplicate_clusters VALUES
            ('cl-1','cxmt','near_duplicate','v-a',2,0.86,'2026-08-02')""")
        db.executemany(
            "INSERT INTO duplicate_cluster_members (cluster_id, version_id) VALUES(?,?)",
            [("cl-1", "v-a"), ("cl-1", "v-b")])
        # A single-source cluster: the same outlet publishing the same template
        # repeatedly, which must not read as corroboration.
        db.execute("""INSERT OR REPLACE INTO document_versions
            (version_id, source_id, company_id, title, published_at)
            VALUES('v-c','media_a','cxmt','翌日披露報表','2026-08-03')""")
        db.execute("""INSERT INTO duplicate_clusters VALUES
            ('cl-2','cxmt','near_duplicate','v-c',1,0.72,'2026-08-04')""")
        db.execute("INSERT INTO duplicate_cluster_members (cluster_id, version_id) VALUES('cl-2','v-c')")
        # Named columns, not positional: this table already exists in the shared
        # fixture with a column set of its own, and a positional insert here
        # breaks the moment either definition gains a field.
        db.execute("""INSERT INTO propagation_components
            (component_id, company_id, member_count, source_count)
            VALUES('pc-1','cxmt',2,2)""")
        db.executemany(
            "INSERT INTO propagation_component_members (component_id, version_id, depth) VALUES(?,?,?)",
            [("pc-1", "v-a", 0), ("pc-1", "v-b", 1)])
        db.commit()
        db.close()


class StatsTests(DuplicateTestCase):
    def test_it_reports_what_the_clustering_folded_away(self):
        # "297 clusters" alone says nothing; the number that means something is
        # how many versions stopped being counted as separate sources.
        self.seed()
        body = self.client.get("/api/duplicates/stats").json()
        self.assertEqual(body["clusters"], 2)
        self.assertEqual(body["members"], 3)
        self.assertEqual(body["collapsed"], 1)

    def test_the_size_histogram_is_ordered_by_size(self):
        self.seed()
        sizes = [row["members"] for row in self.client.get("/api/duplicates/stats").json()["size_histogram"]]
        self.assertEqual(sizes, sorted(sizes))

    def test_the_threshold_travels_with_the_numbers(self):
        # A clustering result without its threshold is not reproducible.
        self.seed()
        threshold = self.client.get("/api/duplicates/stats").json()["threshold"]
        self.assertEqual(threshold["kind"], "near_duplicate")
        self.assertEqual((threshold["low"], threshold["high"]), (0.72, 0.86))

    def test_an_empty_corpus_does_not_divide_by_anything(self):
        body = self.client.get("/api/duplicates/stats").json()
        self.assertEqual(body["clusters"], 0)
        self.assertEqual(body["collapsed"], 0)


class ListTests(DuplicateTestCase):
    def test_the_largest_cluster_comes_first(self):
        self.seed()
        items = self.client.get("/api/duplicates", params={"min_members": 1}).json()["items"]
        self.assertEqual([row["cluster_id"] for row in items], ["cl-1", "cl-2"])

    def test_the_representative_carries_its_document(self):
        self.seed()
        row = self.client.get("/api/duplicates", params={"min_members": 2}).json()["items"][0]
        self.assertEqual(row["representative_title"], "长鑫科技募资 295 亿元")
        self.assertEqual(row["representative_source"], "media_a")

    def test_the_minimum_size_filter_excludes_singletons(self):
        self.seed()
        body = self.client.get("/api/duplicates", params={"min_members": 2}).json()
        self.assertEqual(body["total"], 1)

    def test_the_total_counts_clusters_not_one(self):
        # total_of wraps the query in count(*) FROM (...), so handing it a query
        # that already aggregates counts the aggregate row and always answers 1.
        self.seed()
        self.assertEqual(self.client.get("/api/duplicates", params={"min_members": 1}).json()["total"], 2)


class ClusterDetailTests(DuplicateTestCase):
    def test_two_outlets_carrying_one_story_are_two_independent_sources(self):
        self.seed()
        body = self.client.get("/api/duplicates/cl-1").json()
        self.assertEqual(len(body["members"]), 2)
        self.assertEqual(body["distinct_sources"], 2)
        self.assertEqual(body["sources"], ["media_a", "media_b"])

    def test_one_outlet_repeating_itself_is_one_source(self):
        # The whole point of the count: repetition is not corroboration.
        self.seed()
        self.assertEqual(self.client.get("/api/duplicates/cl-2").json()["distinct_sources"], 1)

    def test_the_propagation_component_is_returned_beside_the_cluster(self):
        # Similarity grouping and copy-relation grouping are different questions
        # about the same versions; showing only one invites reading it as both.
        self.seed()
        body = self.client.get("/api/duplicates/cl-1").json()
        self.assertEqual([row["component_id"] for row in body["components"]], ["pc-1"])

    def test_an_unknown_cluster_answers_emptily_rather_than_failing(self):
        self.seed()
        body = self.client.get("/api/duplicates/nope").json()
        self.assertIsNone(body["cluster"])
        self.assertEqual(body["members"], [])


if __name__ == "__main__":
    unittest.main()
