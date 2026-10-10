"""Evaluation results over the API — 作品提交第 6 项.

Two things are checked beyond "it returns rows".

The headline comes from **test**. 交付物11 fixed that rule for itself, and it
matters more here than in a document, because a page will be read faster than it
will be scrutinised: a train figure quoted without its split is the most
flattering arrangement of the same work.

And a missing database is a state, not an error. The deploy package ships the
corpus and not the benchmark, so an installation without it has to say so rather
than return a 500 — the same discipline the health probe follows for an
unreachable model gateway.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest

from api.errors import reference
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from api import benchmark_db
from tests.test_api import ApiTestCase


def build_benchmark(path: str) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE benchmark_runs(
        run_id TEXT PRIMARY KEY, task TEXT NOT NULL, split TEXT NOT NULL,
        dataset_version TEXT NOT NULL, map_version TEXT, params_json TEXT NOT NULL,
        code_rev TEXT, model_name TEXT, prompt_version TEXT, sample_cap INTEGER,
        started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL, notes TEXT);
    CREATE TABLE benchmark_metrics(
        run_id TEXT NOT NULL, task TEXT NOT NULL, metric TEXT NOT NULL,
        value REAL, support INTEGER, detail_json TEXT,
        PRIMARY KEY(run_id, task, metric));
    CREATE TABLE gold_articles(article_file_id TEXT PRIMARY KEY, split TEXT, sheet TEXT);
    """)
    # The same task on both splits, with the train figure the more flattering
    # one — so a summary that quietly prefers train is visible in the assertion.
    db.executemany("""INSERT INTO benchmark_runs
        (run_id, task, split, dataset_version, params_json, started_at, completed_at, status)
        VALUES(?,?,?,?,?,?,?,?)""", [
        ("r-test", "e5", "test", "v1", "{}", "2026-09-01", "2026-09-01T10:00", "ok"),
        ("r-train", "e5", "train", "v1", "{}", "2026-09-01", "2026-09-01T11:00", "ok"),
        ("r-aborted", "e5", "test", "v1", "{}", "2026-09-02", None, "aborted"),
    ])
    db.executemany("INSERT INTO benchmark_metrics VALUES(?,?,?,?,?,?)", [
        ("r-test", "e5", "b3_f1", 0.965, 1782, None),
        ("r-test", "e5", "pair_f1", 0.827, 1782, None),
        ("r-train", "e5", "b3_f1", 0.9817, 3545, None),
        ("r-test", "e5", "confusion", None, None,
         json.dumps({"SUPPORTED": {"SUPPORTED": 10, "PARTIAL": 2}})),
    ])
    db.executemany("INSERT INTO gold_articles VALUES(?,?,?)",
                   [("a1", "train", "个股新闻"), ("a2", "test", "个股新闻")])
    db.commit()
    db.close()


class BenchmarkTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.benchmark = Path(self.directory.name) / "benchmark.sqlite3"
        build_benchmark(str(self.benchmark))
        patch = mock.patch.object(benchmark_db, "DB_PATH", self.benchmark)
        patch.start()
        self.addCleanup(patch.stop)


class SummaryTests(BenchmarkTestCase):
    def test_the_headline_comes_from_test_even_when_train_looks_better(self):
        task = self.client.get("/api/benchmark/summary").json()["tasks"][0]
        self.assertEqual(task["split"], "test")
        values = {row["metric"]: row["value"] for row in task["headline"]}
        self.assertEqual(values["b3_f1"], 0.965)   # not the 0.9817 from train

    def test_the_split_travels_with_the_number(self):
        # A figure without its split is not a figure anybody can check.
        for task in self.client.get("/api/benchmark/summary").json()["tasks"]:
            self.assertIn(task["split"], {"train", "test"})

    def test_an_aborted_run_is_not_quoted(self):
        task = self.client.get("/api/benchmark/summary").json()["tasks"][0]
        self.assertEqual(task["run_ref"], reference("r-test", "R"))

    def test_run_provenance_stays_on_the_server(self):
        # I-06: dataset and map versions, model and prompt are not published.
        task = self.client.get("/api/benchmark/summary").json()["tasks"][0]
        for hidden in ("run_id", "dataset_version", "map_version", "model_name"):
            self.assertNotIn(hidden, task)


class RunTests(BenchmarkTestCase):
    def test_runs_are_listed_newest_first(self):
        items = self.client.get("/api/benchmark/runs").json()["items"]
        self.assertEqual(items[0]["run_ref"], reference("r-train", "R"))
        self.assertNotIn("run_id", items[0])

    def test_a_run_returns_its_scalars_and_its_detail_payloads_apart(self):
        body = self.client.get(f"/api/benchmark/runs/{reference('r-test', 'R')}").json()
        metrics = {row["metric"] for row in body["scalars"]}
        self.assertEqual(metrics, {"b3_f1", "pair_f1"})
        self.assertIn("confusion", body["details"])
        self.assertEqual(body["details"]["confusion"]["SUPPORTED"]["PARTIAL"], 2)

    def test_an_unknown_run_answers_emptily(self):
        body = self.client.get("/api/benchmark/runs/nope").json()
        self.assertTrue(body["available"])
        self.assertIsNone(body["run"])

    def test_the_task_filter_narrows(self):
        self.assertEqual(len(self.client.get("/api/benchmark/runs",
                                             params={"split": "test"}).json()["items"]), 2)


class DatasetTests(BenchmarkTestCase):
    def test_the_denominator_is_reported(self):
        body = self.client.get("/api/benchmark/dataset").json()
        splits = {row["value"]: row["count"] for row in body["splits"]}
        self.assertEqual(splits, {"train": 1, "test": 1})


class AbsenceTests(ApiTestCase):
    def test_every_route_degrades_rather_than_failing_without_the_database(self):
        # The deploy package ships the corpus and not the benchmark. An
        # installation without it should say so on the page.
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent.sqlite3"
            with mock.patch.object(benchmark_db, "DB_PATH", missing):
                for path in ("/api/benchmark/summary", "/api/benchmark/runs",
                             "/api/benchmark/dataset", "/api/benchmark/runs/anything"):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200, path)
                    body = response.json()
                    self.assertFalse(body["available"], path)
                    self.assertIn("未随本次部署分发", body["reason"], path)


if __name__ == "__main__":
    unittest.main()
