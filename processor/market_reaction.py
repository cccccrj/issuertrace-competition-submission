#!/usr/bin/env python3
"""Check ``impact_direction`` against what the market actually did.

``impact_direction`` is one of the seven outputs the brief requires, and until
the quote series arrived nothing in this system could disagree with it.  A
model said 正面 or 负面, and there was no second opinion in the database — not
a weak check, **no check**.

This adds one.  It is deliberately a *comparison*, not a verification, and the
distinction has to survive into every place the number is shown:

* A price move is not proof of an event's direction.  Markets move for reasons
  that have nothing to do with the document being labelled, and one observation
  cannot separate them.  What agreement measures is whether the label points
  the same way as the tape — useful, falsifiable, and not causal.
* The **excess** return is what is compared, never the raw one.  A stock that
  fell 2% on a day the index fell 3% went up relative to its market, and a raw
  return would have called that a negative reaction.
* Anchoring is on **publication**, not on the event date.  A market reacts when
  it learns something.  Where publication is missing the event time is used and
  the row says so, because the two are different measurements and averaging
  them would hide which one produced the number.

The neutral band is measured, not chosen.  ``σ`` is the standard deviation of
that symbol's own daily excess return over the sample, and a move smaller than
one σ is recorded as **no discernible reaction** rather than being forced into
a direction.  Those rows stay in the table and out of the agreement
denominator, and both counts are reported: a rate whose denominator moved
silently is the failure this project keeps finding in its own history.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from statistics import pstdev

from collector.collect import audit, connect, now_iso

HORIZONS = (1, 3, 5)
# Below one standard deviation of the symbol's own daily excess return, this
# refuses to call a direction at all.  Any fixed percentage would be a constant
# invented for one stock and applied to another with different volatility.
SIGNIFICANCE_SIGMAS = 1.0


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS event_market_reaction (
        event_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, symbol TEXT NOT NULL,
        benchmark_symbol TEXT NOT NULL, anchor_date TEXT NOT NULL, anchor_basis TEXT NOT NULL,
        baseline_date TEXT NOT NULL, baseline_close REAL NOT NULL,
        car_1 REAL, car_3 REAL, car_5 REAL, sigma REAL NOT NULL,
        impact_direction TEXT, observed_direction TEXT NOT NULL,
        significant INTEGER NOT NULL, agrees INTEGER, computed_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_reaction_company ON event_market_reaction(company_id)")
    db.commit()


def series(db: sqlite3.Connection, symbol: str) -> list[tuple[str, float]]:
    return [(row[0], row[1]) for row in db.execute(
        "SELECT trade_date, close FROM market_quotes WHERE symbol=? ORDER BY trade_date", (symbol,))]


def excess_returns(stock: list[tuple[str, float]],
                   benchmark: dict[str, float]) -> dict[str, float]:
    """Daily return of the stock minus the same day's return of its index."""
    excess: dict[str, float] = {}
    for index in range(1, len(stock)):
        day, close = stock[index]
        previous_day, previous_close = stock[index - 1]
        if not previous_close or day not in benchmark or previous_day not in benchmark:
            continue
        market = benchmark[previous_day]
        if not market:
            continue
        excess[day] = (close / previous_close - 1) - (benchmark[day] / market - 1)
    return excess


def anchor_index(days: list[str], target: str) -> int | None:
    """Position of the first trading day on or after ``target``."""
    for index, day in enumerate(days):
        if day >= target:
            return index
    return None


def cumulative(excess: dict[str, float], days: list[str], start: int, horizon: int) -> float | None:
    """Excess return accumulated over ``horizon`` trading days from the anchor."""
    window = days[start:start + horizon]
    if len(window) < horizon:
        return None
    total = 0.0
    for day in window:
        if day not in excess:
            return None
        total += excess[day]
    return total


def observed(car: float | None, sigma: float) -> tuple[str, bool]:
    if car is None:
        return "unknown", False
    if sigma <= 0 or abs(car) < SIGNIFICANCE_SIGMAS * sigma:
        return "none", False
    return ("positive", True) if car > 0 else ("negative", True)


def run(companies: set[str] | None = None) -> dict[str, object]:
    db = connect()
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_quotes'").fetchone():
        db.close()
        return {"available": False, "reason": "market_quotes 未采集，先运行 collector.market_quotes"}

    listings: dict[str, sqlite3.Row] = {}
    for row in db.execute("""SELECT * FROM market_symbols WHERE kind='equity'
                             ORDER BY CASE market WHEN 'sh' THEN 0 WHEN 'sz' THEN 1 ELSE 2 END"""):
        # One listing scored per company.  A+H issuers get their mainland line,
        # because that is where the disclosure this corpus collects is filed;
        # the other listing stays in market_quotes, unscored and available.
        listings.setdefault(row["company_id"], row)

    cache: dict[str, tuple[list[str], dict[str, float], float]] = {}
    for company_id, listing in listings.items():
        stock = series(db, listing["symbol"])
        benchmark = dict(series(db, listing["benchmark_symbol"]))
        excess = excess_returns(stock, benchmark)
        days = [day for day, _ in stock]
        sigma = pstdev(excess.values()) if len(excess) > 2 else 0.0
        cache[company_id] = (days, excess, sigma)

    query = """SELECT e.event_id, e.company_id, e.impact_direction, e.event_time,
                      v.published_at
               FROM event_records e JOIN document_versions v ON v.version_id=e.version_id"""
    params: list[str] = []
    if companies:
        query += " WHERE e.company_id IN (%s)" % ",".join("?" * len(companies))
        params = sorted(companies)

    counts = {"events": 0, "matched": 0, "no_listing": 0, "no_date": 0, "outside_series": 0,
              "not_significant": 0, "scored": 0, "agree": 0}
    db.execute("DELETE FROM event_market_reaction")
    for event in db.execute(query, params):
        counts["events"] += 1
        listing = listings.get(event["company_id"])
        if listing is None:
            counts["no_listing"] += 1
            continue
        # Publication first: a market reacts when it learns something, not when
        # the thing happened.  Which one was used travels with the row.
        anchor_basis = "published_at" if event["published_at"] else "event_time"
        anchor = (event["published_at"] or event["event_time"] or "")[:10]
        if len(anchor) != 10:
            counts["no_date"] += 1
            continue
        days, excess, sigma = cache[event["company_id"]]
        start = anchor_index(days, anchor)
        if start is None or start == 0:
            # Before the listing existed, or after the last quote. Not a defect:
            # one of these issuers listed in 2026 and most of its corpus predates
            # its own ticker.
            counts["outside_series"] += 1
            continue
        cars = {horizon: cumulative(excess, days, start, horizon) for horizon in HORIZONS}
        direction, significant = observed(cars[1], sigma)
        stated = event["impact_direction"]
        # `uncertain` is a refusal to call it, and scoring it either way would
        # turn a declined answer into a wrong one.
        scorable = significant and stated in {"positive", "negative", "neutral"} and stated != "neutral"
        agrees = None
        if scorable:
            agrees = int(direction == stated)
            counts["scored"] += 1
            counts["agree"] += agrees
        elif not significant:
            counts["not_significant"] += 1
        db.execute("""INSERT INTO event_market_reaction
            (event_id,company_id,symbol,benchmark_symbol,anchor_date,anchor_basis,baseline_date,
             baseline_close,car_1,car_3,car_5,sigma,impact_direction,observed_direction,
             significant,agrees,computed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event["event_id"], event["company_id"], listing["symbol"], listing["benchmark_symbol"],
             days[start], anchor_basis, days[start - 1],
             db.execute("SELECT close FROM market_quotes WHERE symbol=? AND trade_date=?",
                        (listing["symbol"], days[start - 1])).fetchone()[0],
             cars[1], cars[3], cars[5], sigma, stated, direction, int(significant), agrees,
             now_iso()))
        counts["matched"] += 1
    db.commit()
    counts["agreement_rate"] = round(counts["agree"] / counts["scored"], 4) if counts["scored"] else None
    counts["coverage"] = round(counts["matched"] / counts["events"], 4) if counts["events"] else None
    counts["available"] = True
    db.close()
    audit({"source_id": "system", "outcome": "market_reaction_computed", **counts})
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare impact_direction with the market's excess return (a consistency check, not a cause)")
    parser.add_argument("--company", action="append")
    args = parser.parse_args()
    print(json.dumps(run(set(args.company) if args.company else None), ensure_ascii=False))


if __name__ == "__main__":
    main()
