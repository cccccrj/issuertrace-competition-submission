"""The one external check on ``impact_direction``, and its limits.

Every assertion here defends a distinction that is easy to lose and expensive
to lose: excess return versus raw return, publication versus event date,
「no discernible move」 versus 「neutral」, and a declined answer versus a wrong
one.  Each of those, collapsed, would produce a number that looks like a
measurement and is not one.
"""
from __future__ import annotations

import unittest

from processor.market_reaction import (SIGNIFICANCE_SIGMAS, anchor_index, cumulative,
                                       excess_returns, observed)


class ExcessReturnTests(unittest.TestCase):
    def test_the_market_move_is_removed(self):
        # A stock that fell 2% on a day its index fell 3% rose against its
        # market.  A raw return calls that a negative reaction.
        stock = [("d1", 100.0), ("d2", 98.0)]
        benchmark = {"d1": 100.0, "d2": 97.0}
        self.assertAlmostEqual(excess_returns(stock, benchmark)["d2"], 0.01, places=6)

    def test_a_day_the_benchmark_did_not_trade_is_skipped(self):
        # Hong Kong and the mainland do not share a holiday calendar; pairing a
        # stock day against a stale index day invents a return.
        stock = [("d1", 100.0), ("d2", 110.0)]
        self.assertEqual(excess_returns(stock, {"d1": 100.0}), {})

    def test_a_flat_market_leaves_the_stock_return_alone(self):
        stock = [("d1", 100.0), ("d2", 105.0)]
        self.assertAlmostEqual(excess_returns(stock, {"d1": 10.0, "d2": 10.0})["d2"], 0.05, places=6)


class AnchoringTests(unittest.TestCase):
    DAYS = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]

    def test_news_on_a_trading_day_anchors_to_that_day(self):
        self.assertEqual(anchor_index(self.DAYS, "2026-01-05"), 1)

    def test_news_on_a_weekend_anchors_to_the_next_open(self):
        # Published Saturday the 3rd; the market's first chance to react is the
        # 5th.  Anchoring to the previous close would score the reaction a day
        # before the information existed.
        self.assertEqual(anchor_index(self.DAYS, "2026-01-03"), 1)

    def test_news_after_the_last_quote_has_no_anchor(self):
        self.assertIsNone(anchor_index(self.DAYS, "2026-02-01"))

    def test_news_before_the_series_begins_anchors_at_zero(self):
        # Index 0 has no prior close to measure from, and the caller drops it.
        # One issuer here listed in 2026 and most of its corpus predates its
        # own ticker; that gap is reported, never quietly filled.
        self.assertEqual(anchor_index(self.DAYS, "2020-01-01"), 0)


class CumulativeTests(unittest.TestCase):
    DAYS = ["d1", "d2", "d3", "d4"]
    EXCESS = {"d1": 0.01, "d2": 0.02, "d3": -0.01, "d4": 0.03}

    def test_one_day_is_the_anchor_day_itself(self):
        self.assertAlmostEqual(cumulative(self.EXCESS, self.DAYS, 1, 1), 0.02, places=6)

    def test_three_days_accumulate_from_the_anchor(self):
        self.assertAlmostEqual(cumulative(self.EXCESS, self.DAYS, 1, 3), 0.04, places=6)

    def test_a_window_running_past_the_series_is_none_not_a_short_sum(self):
        # A five-day window scored over three days is not a five-day number.
        self.assertIsNone(cumulative(self.EXCESS, self.DAYS, 2, 5))

    def test_a_missing_day_inside_the_window_voids_it(self):
        self.assertIsNone(cumulative({"d2": 0.02}, self.DAYS, 1, 2))


class SignificanceTests(unittest.TestCase):
    SIGMA = 0.02

    def test_a_move_larger_than_sigma_gets_a_direction(self):
        self.assertEqual(observed(0.05, self.SIGMA), ("positive", True))
        self.assertEqual(observed(-0.05, self.SIGMA), ("negative", True))

    def test_a_move_smaller_than_sigma_is_no_reaction_not_a_neutral_one(self):
        # 「no discernible move」 and 「the market judged this neutral」 are
        # different claims. Only the first is supported by a small number.
        self.assertEqual(observed(0.001, self.SIGMA), ("none", False))

    def test_the_band_scales_with_the_symbol_not_with_a_constant(self):
        # The same 3% move is signal for a quiet stock and noise for a volatile
        # one.  Any fixed percentage would be one stock's constant applied to
        # another.
        self.assertTrue(observed(0.03, 0.01)[1])
        self.assertFalse(observed(0.03, 0.10)[1])

    def test_a_missing_window_is_unknown_rather_than_flat(self):
        self.assertEqual(observed(None, self.SIGMA), ("unknown", False))

    def test_a_symbol_with_no_variance_never_claims_significance(self):
        # Dividing by a zero band would make every rounding error a signal.
        self.assertEqual(observed(0.5, 0.0), ("none", False))

    def test_the_threshold_is_one_measured_sigma(self):
        self.assertEqual(SIGNIFICANCE_SIGMAS, 1.0)


if __name__ == "__main__":
    unittest.main()
