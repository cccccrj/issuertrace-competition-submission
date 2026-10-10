import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("gpu_benchmark", Path(__file__).resolve().parents[1] / "scripts/gpu/submission_benchmark.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class GpuSubmissionTests(unittest.TestCase):
    def test_linear_percentile_and_std(self):
        result = benchmark.distribution([3, 1, 2])
        self.assertEqual(result["mean"], 2)
        self.assertEqual(result["std_sample"], 1)
        self.assertEqual(result["p95"], 2.9)

    def test_full_company_scoped_neighbors(self):
        result = benchmark.validate_neighbors([dict(id="q", hits=[[1, .9], [2, .8]])],
            [dict(id="q", company="a")], {1:"a", 2:"a", 3:"b"}, 2)
        self.assertTrue(result["passed"])

    def test_cross_company_rejected(self):
        with self.assertRaises(AssertionError):
            benchmark.validate_neighbors([dict(id="q", hits=[[2, .9]])],
                [dict(id="q", company="a")], {1:"a", 2:"b"}, 1)

    def test_missing_query_rejected(self):
        with self.assertRaises(AssertionError):
            benchmark.validate_neighbors([], [dict(id="q", company="a")], {1:"a"}, 1)

    def test_unsorted_scores_rejected(self):
        with self.assertRaises(AssertionError):
            benchmark.validate_neighbors([dict(id="q", hits=[[1, .1], [2, .9]])],
                [dict(id="q", company="a")], {1:"a", 2:"a"}, 2)

    def test_nonfinite_scores_rejected(self):
        with self.assertRaises(AssertionError):
            benchmark.validate_neighbors([dict(id="q", hits=[[1, float("nan")]])],
                [dict(id="q", company="a")], {1:"a"}, 1)

    def test_duplicate_candidate_rejected(self):
        with self.assertRaises(AssertionError):
            benchmark.validate_neighbors([dict(id="q", hits=[[1, .9], [1, .8]])],
                [dict(id="q", company="a")], {1:"a", 2:"a"}, 2)

    def test_fp16_rounding_tolerance(self):
        result = benchmark.validate_neighbors([dict(id="q", hits=[[1, 1.001]])],
            [dict(id="q", company="a")], {1:"a"}, 1)
        self.assertTrue(result["passed"])
        self.assertEqual(result["score_max"], 1.001)

    def test_outside_fp16_tolerance_rejected(self):
        with self.assertRaises(AssertionError):
            benchmark.validate_neighbors([dict(id="q", hits=[[1, 1.1]])],
                [dict(id="q", company="a")], {1:"a"}, 1)
