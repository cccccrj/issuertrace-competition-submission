#!/usr/bin/env python3
"""主要会计数据 lifted out of the periodic reports, as numbers.

Why.  可信度评估 has four inputs and every one of them is about the *document* —
who published it, how independent the sources are, how the piece was produced,
what the verdicts said.  Nothing in the system knows anything about the company
underneath.  27 periodic reports have been in the corpus the whole time with
their headline figures on page three, and no query has ever read one.

What is taken.  Exactly the table 主要会计数据 publishes, and nothing else.  The
plan for this named 营业收入/净利润/毛利率/研发费用 — two of which are not in
that table at all, because the list was written without opening a report.  What
is here is the table's own row set: assets, equity, revenue, profit before tax,
net profit, net profit excluding non-recurring items, operating cash flow, ROE
and earnings per share.

What is *not* taken.  Only the current period.  Each table also carries one or
two prior columns, and attributing those to a period means trusting the column
order — an assumption this cannot check from inside a single document, so it is
not made.  The prior periods are already covered anyway: they are the current
column of an earlier report.

Only 摘要 and quarterly reports are read.  A full annual report restates these
same figures and says 营业收入 in about thirty other places, so scoping to the
short document is both simpler and safer than trying to find one table inside
three hundred pages.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from typing import Any

from collector.collect import ROOT, audit, connect, now_iso

# Label -> (stored name, unit).  Two spellings of shareholders' equity because
# the quarterly form says 所有者权益 where the interim form says 净资产.
METRICS: dict[str, tuple[str, str]] = {
    "总资产": ("total_assets", "CNY"),
    "归属于上市公司股东的净资产": ("equity_attributable", "CNY"),
    "归属于上市公司股东的所有者权益": ("equity_attributable", "CNY"),
    "营业收入": ("revenue", "CNY"),
    "利润总额": ("profit_before_tax", "CNY"),
    "归属于上市公司股东的净利润": ("net_profit_attributable", "CNY"),
    "归属于上市公司股东的扣除非经常性损益的净利润": ("net_profit_ex_nonrecurring", "CNY"),
    "经营活动产生的现金流量净额": ("operating_cash_flow", "CNY"),
    "加权平均净资产收益率": ("weighted_roe", "percent"),
    "基本每股收益": ("basic_eps", "CNY_per_share"),
    "稀释每股收益": ("diluted_eps", "CNY_per_share"),
}
METRIC_LABELS = {
    "total_assets": "总资产", "equity_attributable": "归属于上市公司股东的净资产",
    "revenue": "营业收入", "profit_before_tax": "利润总额",
    "net_profit_attributable": "归属于上市公司股东的净利润",
    "net_profit_ex_nonrecurring": "扣非归母净利润",
    "operating_cash_flow": "经营活动现金流量净额",
    "weighted_roe": "加权平均净资产收益率", "basic_eps": "基本每股收益",
    "diluted_eps": "稀释每股收益",
}

NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
# A row's columns, which do not reliably arrive one per line: some filings put
# two cells on the same line («73,669,349,307.12 64,690,326,746.96»), and a
# pattern anchored to a whole line missed every one of them.
VALUE_LINE = re.compile(r"^-?\d[\d,]*(?:\.\d+)?(?:\s+-?\d[\d,]*(?:\.\d+)?)*$")
# Where the headline table ends and the per-quarter breakdown begins. It names
# the same metrics, so anything that falls through to it comes back with a
# quarter's figure wearing the year's label — which is how 2023 revenue was
# briefly recorded as 89.64亿 instead of 403.41亿.
QUARTERLY_TABLE = re.compile(r"分季度的主要会计数据|报告期分季度")
# What sits in a value column when there is no comparable figure.  These are not
# labels and must not be mistaken for the start of the next row.
FILLER = re.compile(r"^(不适用|—|-|–|/|不适用□?|√?适用)$|个百分点$|^单位[:：]|^币种[:：]")
# Trailing unit annotations that are part of the header, not of the name.
# Metric names contain no brackets, so everything from the first one onwards is
# a unit note or a footnote marker. Cutting there also survives the note being
# split across lines: 「基本每股收益（元」 / 「／股）」 arrives as two lines and a
# rule that only removed the complete 「（元／股）」 matched neither half.
LABEL_NOISE = re.compile(r"[（(].*$")

PERIOD_PATTERNS = (
    (re.compile(r"(20\d{2})\s*年\s*年度报告"), "annual", "{year}"),
    (re.compile(r"(20\d{2})\s*年\s*半年度报告"), "interim", "{year}H1"),
    (re.compile(r"(20\d{2})\s*年\s*第一季度报告"), "quarterly", "{year}Q1"),
    (re.compile(r"(20\d{2})\s*年\s*第三季度报告"), "quarterly", "{year}Q3"),
)


def period_of(title: str) -> tuple[str, str] | None:
    """The reporting period a document covers, read off its own title.

    Nothing is inferred from the publication date: an annual report is filed in
    the following March, and a period taken from ``published_at`` would be off
    by a year for every one of them.
    """
    for pattern, kind, template in PERIOD_PATTERNS:
        match = pattern.search(title or "")
        if match:
            return template.format(year=match.group(1)), kind
    return None


def clean_label(parts: list[str]) -> str:
    """Rejoin a label that the PDF broke across lines, and drop its unit note."""
    return LABEL_NOISE.sub("", re.sub(r"\s+", "", "".join(parts)))


def is_prefix(candidate: str) -> bool:
    """Whether this could still grow into a metric label."""
    return bool(candidate) and any(label.startswith(candidate) for label in METRICS)


def parse_indicators(text: str) -> dict[str, tuple[str, float]]:
    """Pull 主要会计数据 out of one report's text.

    The table arrives one cell per line, so a row is a label — possibly wrapped
    over two or three lines — followed by its columns.  The first number after a
    recognised label is the current period; the rest of that run is prior
    periods and change rates and is skipped.

    First match wins.  An annual summary carries a second, per-quarter table
    naming the same metrics further down, and the headline table comes first.
    """
    # Stop before the per-quarter breakdown rather than let a failure upstream
    # fall through into it. Yielding nothing is better than yielding a quarter's
    # figure under the year's name.
    cut = QUARTERLY_TABLE.search(text)
    if cut:
        text = text[:cut.start()]
    found: dict[str, tuple[str, float]] = {}
    parts: list[str] = []
    consuming = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if VALUE_LINE.match(line):
            if not consuming:
                name = METRICS.get(clean_label(parts))
                if name and name[0] not in found:
                    # The first number of the row is the current period; the
                    # rest of the run is prior periods and change rates.
                    found[name[0]] = (name[1], float(NUMBER.search(line).group().replace(",", "")))
                consuming = True
                parts = []
            continue
        if FILLER.search(line):
            # A blank column, not the next row's label.
            continue
        if consuming:
            consuming = False
            parts = []
        # Only keep accumulating while what has accumulated could still *become*
        # a label. A wrapped label is always a prefix of the whole one, so this
        # both joins 归属于上市公司股东 to 的净利润 and refuses to join anything
        # to the column headers that sit between two sub-tables — 本报告期 /
        # 上年同期 / 本报告期比上年同期增减(%). Without that refusal the header
        # swallowed the row beneath it and 营业收入 was found in 1 report of 12.
        if parts and is_prefix(clean_label(parts + [line])):
            parts.append(line)
        elif is_prefix(clean_label([line])):
            parts = [line]
        else:
            parts = []
    return found


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS financial_indicators (
        company_id TEXT NOT NULL, period TEXT NOT NULL, period_type TEXT NOT NULL,
        metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL,
        version_id TEXT NOT NULL, source_title TEXT, published_at TEXT,
        extracted_at TEXT NOT NULL,
        PRIMARY KEY(company_id, period, metric))""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_financials_company
                  ON financial_indicators(company_id, period)""")
    db.commit()


def candidates(db: sqlite3.Connection, companies: set[str] | None) -> list[sqlite3.Row]:
    query = """SELECT v.version_id, v.company_id, v.title, v.published_at, p.text_path
        FROM document_versions v JOIN processed_documents p USING(version_id)
        WHERE p.extraction_status='ok' AND p.page_count IS NOT NULL AND p.text_path IS NOT NULL
          AND v.company_id IS NOT NULL
          AND (v.title LIKE '%报告摘要' OR v.title LIKE '%季度报告')"""
    params: list[str] = []
    if companies:
        query += " AND v.company_id IN (%s)" % ",".join("?" * len(companies))
        params = sorted(companies)
    return db.execute(query + " ORDER BY v.company_id, v.published_at", params).fetchall()


def run(companies: set[str] | None = None) -> dict[str, Any]:
    db = connect()
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    counts = {"documents": 0, "parsed": 0, "no_period": 0, "no_table": 0, "rows": 0}
    periods: list[dict[str, Any]] = []
    for row in candidates(db, companies):
        counts["documents"] += 1
        period = period_of(row["title"])
        if not period:
            counts["no_period"] += 1
            continue
        path = ROOT / row["text_path"]
        if not path.exists():
            continue
        found = parse_indicators(path.read_text(encoding="utf-8"))
        if not found:
            counts["no_table"] += 1
            continue
        counts["parsed"] += 1
        for metric, (unit, value) in found.items():
            db.execute("""INSERT INTO financial_indicators
                (company_id,period,period_type,metric,value,unit,version_id,source_title,
                 published_at,extracted_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(company_id,period,metric) DO UPDATE SET value=excluded.value,
                unit=excluded.unit,version_id=excluded.version_id,
                source_title=excluded.source_title,published_at=excluded.published_at,
                extracted_at=excluded.extracted_at""",
                (row["company_id"], period[0], period[1], metric, value, unit,
                 row["version_id"], row["title"], row["published_at"], now_iso()))
            counts["rows"] += 1
        periods.append({"company_id": row["company_id"], "period": period[0],
                        "metrics": len(found), "title": row["title"]})
    db.commit()
    counts["periods"] = periods
    counts["inconsistent"] = check_cumulative(db)
    db.close()
    audit({"source_id": "system", "outcome": "financial_indicators_extracted",
           **{key: value for key, value in counts.items() if key != "periods"}})
    return counts


def check_cumulative(db: sqlite3.Connection) -> list[str]:
    """Revenue must not shrink from Q1 to H1 to the full year.

    Chinese interim reporting is cumulative within the year, so for any year
    where all three exist the figures have to be ordered.  This is the check
    that catches the failure this parser actually had: a filing whose table put
    two cells on one line fell through to the per-quarter breakdown below it,
    and Q1 revenue was stored as the year's — 89.64亿 where the answer is
    403.41亿, a number that is wrong by a factor of four and looks entirely
    plausible sitting in a column.
    """
    rows = db.execute("""SELECT company_id, period, value FROM financial_indicators
                         WHERE metric='revenue'""").fetchall()
    by_company: dict[str, dict[str, float]] = {}
    for row in rows:
        by_company.setdefault(row["company_id"], {})[row["period"]] = row["value"]
    problems: list[str] = []
    for company_id, periods in by_company.items():
        for year in {period[:4] for period in periods}:
            ladder = [(name, periods.get(name)) for name in
                      (f"{year}Q1", f"{year}H1", year)]
            seen = [(name, value) for name, value in ladder if value is not None]
            for (earlier, small), (later, large) in zip(seen, seen[1:]):
                if small > large:
                    problems.append(f"{company_id} {earlier}({small:,.0f}) > {later}({large:,.0f})")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="主要会计数据 from periodic reports")
    parser.add_argument("--company", action="append")
    args = parser.parse_args()
    print(json.dumps(run(set(args.company) if args.company else None), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
