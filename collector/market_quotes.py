#!/usr/bin/env python3
"""Daily closing prices for the monitored issuers and their benchmark indices.

Why this exists.  ``impact_direction`` is one of the seven outputs the brief
requires, and until now nothing in the system could contradict it: the model
said 正面 or 负面 and there was no second opinion anywhere in the database.
A price series does not make the field *true*, but it makes it **falsifiable**,
which is the property it was missing.

Why this is not a document.  Everything else the collector fetches is evidence
with an author and a publication date, and it lands in ``documents`` /
``document_versions``.  A price series has neither, and putting two thousand
rows of it into the evidence corpus would move every corpus statistic in this
project's own reports.  So this module reuses the collector's *discipline* --
robots, per-host rate limiting, raw response preserved before parsing, one
audit line per fetch -- and writes to a table of its own.

Why this source.  Three public endpoints serve free daily bars.  Eastmoney's
``robots.txt`` is ``Disallow: /`` for every agent, so it is out; this project
does not read what a site has asked it not to read, and «能不暴露就不暴露» has
a twin in «能不采就不采».  Of the remaining two, one endpoint covers all four
symbols this needs -- Shanghai, Hong Kong and an index -- so it is the one that
gets asked, once per symbol.

Forward adjustment (``qfq``) matters and is requested for the A shares: an
unadjusted series steps at every dividend and split, and a step is exactly what
a reaction check would read as a reaction.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import yaml

from collector.collect import (
    ROOT, audit, connect, load_sources, now_iso, reserve_host_slot, robot_allowed,
    save_raw, session_for,
)

SOURCE_ID = "market_quotes"
ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
# The exchange prefixes this endpoint uses.  ``stock_codes`` in sources.yaml
# names the exchange; this maps those names onto the prefix.
EXCHANGE_PREFIX = {"SSE": "sh", "SSE STAR": "sh", "SZSE": "sz", "HKEX": "hk"}
# Benchmarks, one per market the issuers trade in.  A raw return says how the
# whole market moved that day; only the excess says anything about the event.
BENCHMARKS = {"sh": ("sh000300", "沪深300"), "hk": ("hkHSI", "恒生指数")}
BACKFILL_START = "2024-01-01"


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS market_quotes (
        symbol TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL, high REAL, low REAL,
        close REAL NOT NULL, volume REAL, adjustment TEXT NOT NULL, currency TEXT,
        source_id TEXT NOT NULL, raw_path TEXT NOT NULL, fetched_at TEXT NOT NULL,
        PRIMARY KEY(symbol, trade_date))""")
    db.execute("""CREATE TABLE IF NOT EXISTS market_symbols (
        symbol TEXT PRIMARY KEY, kind TEXT NOT NULL, company_id TEXT, label TEXT NOT NULL,
        market TEXT NOT NULL, benchmark_symbol TEXT, adjustment TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_market_quotes_date ON market_quotes(trade_date)")
    db.commit()


def configured_symbols(config: dict) -> list[dict]:
    """Every symbol worth fetching, derived from the monitored entities.

    Nothing here is hard-coded to a company: adding an issuer to
    ``sources.yaml`` adds its listings, and its market's benchmark comes along
    with them.
    """
    symbols: list[dict] = []
    markets: set[str] = set()
    for company_id, entity in (config.get("entities") or {}).items():
        for listing in entity.get("stock_codes") or []:
            prefix = EXCHANGE_PREFIX.get(str(listing.get("exchange", "")).strip())
            if not prefix:
                continue
            code = str(listing.get("code", "")).strip()
            if not code:
                continue
            markets.add(prefix)
            # Hong Kong codes are quoted five digits wide on this endpoint.
            padded = code.zfill(5) if prefix == "hk" else code
            symbols.append({
                "symbol": f"{prefix}{padded}", "kind": "equity", "company_id": company_id,
                "label": f"{entity.get('canonical_name', company_id)} {code}",
                "market": prefix, "benchmark_symbol": BENCHMARKS[prefix][0],
                # Only A shares are served forward-adjusted by this endpoint.
                "adjustment": "qfq" if prefix in {"sh", "sz"} else "none",
            })
    for market in sorted(markets):
        symbol, label = BENCHMARKS[market]
        symbols.append({"symbol": symbol, "kind": "benchmark", "company_id": None,
                        "label": label, "market": market, "benchmark_symbol": None,
                        "adjustment": "none"})
    return symbols


def register_symbols(db, symbols: list[dict]) -> None:
    for entry in symbols:
        db.execute("""INSERT INTO market_symbols
            (symbol,kind,company_id,label,market,benchmark_symbol,adjustment)
            VALUES(:symbol,:kind,:company_id,:label,:market,:benchmark_symbol,:adjustment)
            ON CONFLICT(symbol) DO UPDATE SET kind=excluded.kind,company_id=excluded.company_id,
            label=excluded.label,market=excluded.market,
            benchmark_symbol=excluded.benchmark_symbol,adjustment=excluded.adjustment""", entry)
    db.commit()


def parse_bars(payload: dict, symbol: str) -> list[tuple[str, float, float, float, float, float]]:
    """Pull the daily bars out of one response.

    The adjusted and unadjusted series arrive under different keys, and the
    endpoint answers with ``code: 0`` and an empty body for a symbol it does not
    know -- which is a failure that looks like a success, so it is checked.
    """
    if payload.get("code") not in (0, "0"):
        raise ValueError(f"quote endpoint refused {symbol}: {payload.get('msg') or payload.get('code')}")
    series = (payload.get("data") or {}).get(symbol) or {}
    rows = None
    for key in ("qfqday", "day", "hfqday"):
        candidate = series.get(key)
        if isinstance(candidate, list) and candidate:
            rows = candidate
            break
    if rows is None:
        raise ValueError(f"quote endpoint returned no daily series for {symbol}")
    bars = []
    for row in rows:
        if len(row) < 5:
            continue
        day, open_, close, high, low = row[0], row[1], row[2], row[3], row[4]
        volume = row[5] if len(row) > 5 else None
        bars.append((str(day), float(open_), float(high), float(low), float(close),
                     float(volume) if volume not in (None, "") else None))
    return bars


def store_bars(db, entry: dict, bars: list, raw_path: str) -> int:
    currency = "HKD" if entry["market"] == "hk" else "CNY"
    fetched = now_iso()
    written = 0
    for day, open_, high, low, close, volume in bars:
        written += db.execute("""INSERT INTO market_quotes
            (symbol,trade_date,open,high,low,close,volume,adjustment,currency,source_id,raw_path,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,trade_date) DO UPDATE SET open=excluded.open,high=excluded.high,
            low=excluded.low,close=excluded.close,volume=excluded.volume,
            adjustment=excluded.adjustment,raw_path=excluded.raw_path,fetched_at=excluded.fetched_at""",
            (entry["symbol"], day, open_, high, low, close, volume, entry["adjustment"],
             currency, SOURCE_ID, raw_path, fetched)).rowcount
    db.commit()
    return written


def fetch_symbol(db, source, session, entry: dict, start: str, end: str, timeout: int,
                 min_interval: float) -> int:
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    url = (f"{ENDPOINT}?param={entry['symbol']},day,{start},{end},"
           f"{max(span, 1)},{entry['adjustment'] if entry['adjustment'] != 'none' else ''}")
    host = urlparse(url).hostname or ""
    reserve_host_slot(db, host, SOURCE_ID, min_interval)
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    body = response.content
    # 原文先落盘：the response is preserved before it is parsed, exactly as a
    # fetched page is, so a parsing change can be replayed against what arrived.
    _, raw_path = save_raw(SOURCE_ID, response, body)
    bars = parse_bars(json.loads(body.decode("utf-8")), entry["symbol"])
    written = store_bars(db, entry, bars, raw_path)
    audit({"source_id": SOURCE_ID, "outcome": "market_quotes_fetched", "symbol": entry["symbol"],
           "kind": entry["kind"], "adjustment": entry["adjustment"], "bars": len(bars),
           "rows_written": written, "start": start, "end": end, "raw_path": raw_path})
    return written


def run(*, start: str = BACKFILL_START, end: str | None = None,
        contact: str | None = None) -> dict[str, object]:
    end = end or datetime.now(timezone.utc).date().isoformat()
    config, sources = load_sources(ROOT / "config" / "sources.yaml", {SOURCE_ID}, None)
    if not sources:
        raise SystemExit(f"{SOURCE_ID} is not configured in config/sources.yaml")
    source = sources[0]
    db = connect()
    ensure_schema(db)
    symbols = configured_symbols(config)
    register_symbols(db, symbols)
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})" if contact else "ListedCompanyEventIntelligence/0.1"
    session = session_for(source, user_agent)
    timeout = int(source.profile["request_timeout_seconds"])
    min_interval = float(source.profile["min_interval_seconds"])
    allowed, note = robot_allowed(session, ENDPOINT, user_agent, timeout,
                                  lambda target: reserve_host_slot(db, urlparse(target).hostname or "",
                                                                   SOURCE_ID, min_interval), db)
    audit({"source_id": SOURCE_ID, "outcome": note, "url": ENDPOINT, "allowed": allowed})
    if not allowed:
        db.close()
        raise SystemExit(f"robots.txt disallows {ENDPOINT}: {note}")
    summary = {"symbols": len(symbols), "rows_written": 0, "failed": []}
    for entry in symbols:
        try:
            summary["rows_written"] += fetch_symbol(db, source, session, entry, start, end,
                                                    timeout, min_interval)
        except Exception as exc:  # one bad symbol must not cost the others
            summary["failed"].append({"symbol": entry["symbol"], "error": str(exc)[:200]})
            audit({"source_id": SOURCE_ID, "outcome": "market_quotes_failed",
                   "symbol": entry["symbol"], "error": str(exc)[:300]})
    summary["quotes"] = db.execute("SELECT count(*) FROM market_quotes").fetchone()[0]
    db.close()
    return summary


def main() -> None:
    import os
    parser = argparse.ArgumentParser(description="Daily closes for the monitored issuers and their benchmarks")
    parser.add_argument("--start", default=BACKFILL_START)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()
    print(json.dumps(run(start=args.start, end=args.end,
                         contact=os.environ.get("COLLECTOR_CONTACT")), ensure_ascii=False))


if __name__ == "__main__":
    main()
