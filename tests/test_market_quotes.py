"""The quote collector: what it fetches, and what it refuses to.

Prices are the only external check this system has on ``impact_direction``,
which is one of the seven outputs the brief requires.  Two properties matter
beyond "it returns rows": the symbols must be derived from the monitored
entities rather than written into the code, and the excess-return machinery
must never compare a stock with itself.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from collector.market_quotes import BENCHMARKS, configured_symbols, ensure_schema, parse_bars, store_bars

CONFIG = {
    "entities": {
        "alpha": {"canonical_name": "甲公司", "stock_codes": [{"exchange": "SSE STAR", "code": "688825"}]},
        "beta": {"canonical_name": "乙公司", "stock_codes": [{"exchange": "SSE", "code": "603259"},
                                                            {"exchange": "HKEX", "code": "2359"}]},
    }
}


class SymbolDerivationTests(unittest.TestCase):
    def test_symbols_come_from_the_entity_inventory(self):
        # Adding an issuer to sources.yaml must be enough; nothing here names a
        # company, which is the same discipline the processing layer is held to.
        symbols = {entry["symbol"] for entry in configured_symbols(CONFIG)}
        self.assertIn("sh688825", symbols)
        self.assertIn("sh603259", symbols)

    def test_a_hong_kong_code_is_padded_to_five_digits(self):
        # sources.yaml stores 2359; the endpoint wants hk02359.  Unpadded, the
        # request succeeds and returns another company's series.
        symbols = {entry["symbol"] for entry in configured_symbols(CONFIG)}
        self.assertIn("hk02359", symbols)

    def test_each_market_brings_its_own_benchmark(self):
        entries = {entry["symbol"]: entry for entry in configured_symbols(CONFIG)}
        self.assertEqual(entries["sh603259"]["benchmark_symbol"], BENCHMARKS["sh"][0])
        self.assertEqual(entries["hk02359"]["benchmark_symbol"], BENCHMARKS["hk"][0])
        for symbol, _ in BENCHMARKS.values():
            self.assertEqual(entries[symbol]["kind"], "benchmark")

    def test_a_market_nobody_lists_on_gets_no_benchmark(self):
        single = {"entities": {"alpha": {"canonical_name": "甲", "stock_codes": [{"exchange": "SSE", "code": "1"}]}}}
        markets = {entry["market"] for entry in configured_symbols(single)}
        self.assertEqual(markets, {"sh"})

    def test_only_a_shares_are_requested_forward_adjusted(self):
        # An unadjusted series steps at every dividend, and a step is exactly
        # what a reaction check reads as a reaction.
        entries = {entry["symbol"]: entry for entry in configured_symbols(CONFIG)}
        self.assertEqual(entries["sh603259"]["adjustment"], "qfq")
        self.assertEqual(entries["hk02359"]["adjustment"], "none")

    def test_an_unknown_exchange_is_skipped_rather_than_guessed(self):
        odd = {"entities": {"alpha": {"canonical_name": "甲", "stock_codes": [{"exchange": "NYSE", "code": "X"}]}}}
        self.assertEqual(configured_symbols(odd), [])


class ResponseParsingTests(unittest.TestCase):
    def test_the_adjusted_series_is_found_under_its_own_key(self):
        payload = {"code": 0, "data": {"sh603259": {"qfqday": [
            ["2026-08-20", "170.49", "167.39", "173.09", "165.46", "859114"]]}}}
        bars = parse_bars(payload, "sh603259")
        self.assertEqual(bars[0][0], "2026-08-20")
        self.assertEqual(bars[0][4], 167.39)   # close, not open

    def test_the_unadjusted_series_is_found_too(self):
        payload = {"code": 0, "data": {"hk02359": {"day": [["2026-08-20", "208", "206.6", "212.8", "204.8", "1"]]}}}
        self.assertEqual(parse_bars(payload, "hk02359")[0][4], 206.6)

    def test_the_column_order_is_open_close_high_low(self):
        # The endpoint does not send OHLC in that order; reading it as OHLC puts
        # the close where the high belongs and silently shifts every return.
        payload = {"code": 0, "data": {"s": {"day": [["d", "1", "2", "9", "0.5", "7"]]}}}
        day, open_, high, low, close, volume = parse_bars(payload, "s")[0]
        self.assertEqual((open_, high, low, close), (1.0, 9.0, 0.5, 2.0))

    def test_an_empty_series_is_an_error_not_an_empty_result(self):
        # The endpoint answers `code: 0` with no data for a symbol it does not
        # know — a failure shaped like a success.
        with self.assertRaises(ValueError):
            parse_bars({"code": 0, "data": {"sh1": {}}}, "sh1")

    def test_a_refusal_is_reported_with_its_message(self):
        with self.assertRaises(ValueError):
            parse_bars({"code": 11, "msg": "no dispatch info"}, "sh1")


class StorageTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        return db

    def test_refetching_the_same_day_updates_rather_than_duplicates(self):
        db = self._db()
        entry = {"symbol": "sh1", "market": "sh", "adjustment": "qfq"}
        store_bars(db, entry, [("2026-01-02", 1.0, 2.0, 0.5, 1.5, 10.0)], "raw/a")
        store_bars(db, entry, [("2026-01-02", 1.0, 2.0, 0.5, 1.9, 10.0)], "raw/b")
        rows = db.execute("SELECT close, raw_path FROM market_quotes").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 1.9)

    def test_the_raw_response_path_is_kept_with_every_row(self):
        # 原文先落盘 applies here too: a parsing change has to be replayable
        # against what actually arrived.
        db = self._db()
        store_bars(db, {"symbol": "sh1", "market": "sh", "adjustment": "qfq"},
                   [("2026-01-02", 1.0, 2.0, 0.5, 1.5, 10.0)], "data/raw/market_quotes/x.bin")
        self.assertEqual(db.execute("SELECT raw_path FROM market_quotes").fetchone()[0],
                         "data/raw/market_quotes/x.bin")

    def test_currency_follows_the_market(self):
        db = self._db()
        store_bars(db, {"symbol": "hk1", "market": "hk", "adjustment": "none"},
                   [("2026-01-02", 1.0, 2.0, 0.5, 1.5, 10.0)], "raw/a")
        self.assertEqual(db.execute("SELECT currency FROM market_quotes").fetchone()[0], "HKD")


def source_entry() -> dict:
    import yaml
    from collector.market_quotes import SOURCE_ID
    config = yaml.safe_load(Path("config/sources.yaml").read_text(encoding="utf-8"))
    return next(s for s in config["sources"] if s["source_id"] == SOURCE_ID)


class ComplianceTests(unittest.TestCase):
    def test_the_source_declares_it_is_not_evidence(self):
        # A price series has no author and no publication date.  Letting it into
        # the evidence corpus would move every corpus statistic in this
        # project's own reports.
        entry = source_entry()
        self.assertFalse(entry["evidence_eligible"])
        self.assertFalse(entry["paywall"])

    def test_the_note_records_why_the_other_source_was_refused(self):
        # Eastmoney serves the same data and its robots.txt is `Disallow: /` for
        # every agent.  The reason belongs in the config, not in a commit
        # message nobody reads again.
        entry = source_entry()
        self.assertIn("robots", entry["terms_and_robots_note"])


if __name__ == "__main__":
    unittest.main()
