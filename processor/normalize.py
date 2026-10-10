#!/usr/bin/env python3
"""Turn extracted mentions into comparable, standardized values.

命题 pairs 要素抽取 with 要素标准化 for a reason: extraction proves a value was
found, standardization is what makes it sortable, aggregatable and comparable
across documents.  Without it, issue grouping, timelines and cross-company
statistics cannot be computed at all.

This pass is deterministic and offline.  It reads the mentions stored by the
labelling pass, resolves them per §9.3 of the Pro plan, and writes the result to
``event_elements``.  Following that section, a value that cannot be resolved is
left empty and flagged for review rather than guessed, and every normalized
value keeps the raw text it came from.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import yaml

from processor.claim_index import fold

from collector.collect import ROOT, audit, connect
from processor.pipeline import configured_companies, ensure_schema, event_time_role, set_review_reason, utc_now

CONFIG_PATH = ROOT / "config" / "sources.yaml"
ORGANIZATIONS_PATH = ROOT / "config" / "organizations.yaml"

# Expressions that denote the document's own subject rather than naming it.
# Statutory disclosure says 本公司 far more often than it says the company's
# name, and the prompt tells the model to copy the document's wording, so
# "the Company" arrived 33 times and resolved to nothing at all.
#
# 董事会 and "the Board" are deliberately absent.  A board is an organ of a
# company, not the company; resolving one to the other would be inventing an
# equivalence to make a number look better.  They stay unresolved.
ANAPHORA = {"本公司", "该公司", "公司", "本集团", "集团", "发行人",
            "the company", "the group", "the issuer"}

# Multipliers are written as integers so money stays exact in 元/分.
CN_SCALES = {"十亿": 10**9, "亿": 10**8, "千万": 10**7, "百万": 10**6, "万": 10**4, "千": 10**3}
EN_SCALES = {"billion": 10**9, "bn": 10**9, "million": 10**6, "m": 10**6, "thousand": 10**3, "k": 10**3}
CURRENCIES = {
    "人民币": "CNY", "元": "CNY", "rmb": "CNY", "cny": "CNY", "¥": "CNY",
    "美元": "USD", "usd": "USD", "$": "USD", "dollars": "USD", "dollar": "USD",
    "港元": "HKD", "港币": "HKD", "hkd": "HKD",
    "欧元": "EUR", "eur": "EUR", "€": "EUR",
    "日元": "JPY", "jpy": "JPY",
}
# 百分点 vs 百分比 is a documented exaggeration vector (§10), so the distinction
# is resolved here rather than left to the model.
PERCENTAGE_POINT_TERMS = ("百分点", "个点", "percentage point", "percentage points", "pp", "ppt")

QUARTER_MONTHS = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}
CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "两": 2}

# The WuXi AppTec corpus is largely English-language HK filings, so the same
# mention kinds arrive in both languages and both have to resolve to one scale.
MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
MONTH_PATTERN = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
EN_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
EN_COUNTS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "nine": 9, "twelve": 12}

# A weekday, a trailing clock time and a parenthetical aside all decorate a date
# without changing it.  Stripping them first keeps the date patterns themselves
# simple, and it is also what leaves a clock-only mention such as 截至11:00 with
# nothing to resolve — which is the correct outcome, not a parsing failure.
WEEKDAY_PREFIX = re.compile(r"^\s*(?:mon|tues|wednes|thurs|fri|satur|sun)day\s*[,，]?\s*", re.I)
AS_OF_PREFIX = re.compile(r"^\s*as\s+(?:at|of)\s+", re.I)
TIME_OF_DAY = re.compile(r"\s*(?:\bat\s+)?\d{1,2}\s*[:：]\s*\d{2}\s*(?:[ap]\.?\s*m\.?)?\s*$", re.I)
PARENTHETICAL = re.compile(r"[（(][^）)]*[）)]")
RANGE_SPLIT = re.compile(r"\s+(?:to|through|until)\s+|\s*[–—]\s*|\s*至\s*", re.I)


def month_end(year: int, month: int) -> date:
    return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def window_start(anchor: date, months: int) -> date:
    """First day of a window of ``months`` whole months ending in ``anchor``."""
    index = anchor.year * 12 + (anchor.month - 1) - (months - 1)
    return date(index // 12, index % 12 + 1, 1)


def strip_decorations(text: str) -> str:
    """Remove weekday, clock time and parenthetical noise around a date."""
    cleaned = PARENTHETICAL.sub(" ", text)
    cleaned = TIME_OF_DAY.sub("", cleaned)
    cleaned = WEEKDAY_PREFIX.sub("", cleaned)
    cleaned = AS_OF_PREFIX.sub("", cleaned)
    cleaned = re.sub(r"^\s*(?:the|on|in|by)\s+", "", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip(" ,.;:")


def english_date(text: str, published: date | None) -> date | None:
    """Parse ``26 June 2025``, ``March 23, 2026`` and their year-less forms."""
    # ``(?!\d)`` on the day is what keeps "May 2025" from being read as day 20 of
    # May: without it the day group eats the first two digits of the year.
    day_first = re.search(
        rf"(?<!\d)(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?\s+({MONTH_PATTERN})\b\s*,?\s*(\d{{4}})?", text, re.I)
    month_first = re.search(
        rf"\b({MONTH_PATTERN})\b\s+(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?\s*,?\s*(\d{{4}})?", text, re.I)
    if day_first:
        day, month = int(day_first.group(1)), MONTH_NAMES[day_first.group(2).lower()]
        year = int(day_first.group(3)) if day_first.group(3) else (published.year if published else None)
    elif month_first:
        month, day = MONTH_NAMES[month_first.group(1).lower()], int(month_first.group(2))
        year = int(month_first.group(3)) if month_first.group(3) else (published.year if published else None)
    else:
        return None
    if year is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def publication_date(published_at: str | None) -> date | None:
    if not published_at:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(published_at))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _span(start: date, end: date, precision: str, basis: str) -> dict[str, str]:
    if start == end and precision == "day":
        return {"value": start.isoformat(), "precision": "day", "basis": basis}
    return {"value": f"{start.isoformat()}/{end.isoformat()}", "precision": precision, "basis": basis}


def _bounds(resolved: dict[str, str]) -> tuple[date, date]:
    value = resolved["value"]
    if "/" in value:
        left, right = value.split("/", 1)
        return date.fromisoformat(left), date.fromisoformat(right)
    return date.fromisoformat(value), date.fromisoformat(value)


def english_period(text: str, published: date | None) -> dict[str, str] | None:
    """Resolve English quarter/half/reporting-window phrasings.

    These carry a period, not a date: ``first quarter ending March 31, 2024``
    is Q1 2024 as a whole, so the trailing date is the window's end rather than
    the event's day.  Matching them before the plain date patterns is what keeps
    that distinction.
    """
    lowered = text.lower()
    year_match = re.search(r"(?<!\d)(\d{4})(?!\d)", lowered)
    year = int(year_match.group(1)) if year_match else (published.year if published else None)

    compact = re.search(r"(?<!\d)(\d{4})\s*[hH]([12])(?!\d)|(?<![a-z\d])[hH]([12])\s*(\d{4})(?!\d)", text)
    if compact:
        year = int(compact.group(1) or compact.group(4))
        index = int(compact.group(2) or compact.group(3))
        start, end = ((1, 6) if index == 1 else (7, 12))
        return _span(date(year, start, 1), month_end(year, end), "range", "explicit")

    loose_quarter = re.search(r"(?<![a-z\d])[qQ]([1-4])\s*(?:of\s*)?(\d{4})?(?!\d)", text)
    if loose_quarter:
        index = int(loose_quarter.group(1))
        quarter_year = int(loose_quarter.group(2)) if loose_quarter.group(2) else year
        if quarter_year is None:
            return None
        start, end = QUARTER_MONTHS[index]
        basis = "explicit" if loose_quarter.group(2) else "relative_to_publication"
        return _span(date(quarter_year, start, 1), month_end(quarter_year, end), "quarter", basis)

    # "six months ended June 30, 2025" anchors on its own end date, so it is
    # resolved before the generic ordinal forms.
    window = re.search(rf"\b({'|'.join(EN_COUNTS)}|\d{{1,2}})\s+months?\s+end(?:ed|ing)\b", lowered)
    if window:
        token = window.group(1)
        months = EN_COUNTS.get(token) or int(token)
        anchor = english_date(text, published)
        if anchor:
            return _span(window_start(anchor, months), anchor, "range", "explicit")

    if re.search(r"\bfirst\s+three\s+quarters\b", lowered) and year is not None:
        return _span(date(year, 1, 1), month_end(year, 9), "range",
                     "explicit" if year_match else "relative_to_publication")
    if re.search(r"\bfirst\s+six\s+months\b", lowered) and year is not None:
        return _span(date(year, 1, 1), month_end(year, 6), "range",
                     "explicit" if year_match else "relative_to_publication")

    ordinal = re.search(rf"\b({'|'.join(EN_ORDINALS)})[\s-]+(quarter|half)\b", lowered)
    if not ordinal:
        ordinal = re.search(rf"\b(quarter|half)\b.*?\b({'|'.join(EN_ORDINALS)})\b", lowered)
    if ordinal and year is not None:
        groups = ordinal.groups()
        index = EN_ORDINALS[groups[0] if groups[0] in EN_ORDINALS else groups[1]]
        kind = groups[1] if groups[0] in EN_ORDINALS else groups[0]
        basis = "explicit" if year_match else "relative_to_publication"
        if kind == "half":
            if index > 2:
                return None
            start, end = ((1, 6) if index == 1 else (7, 12))
            return _span(date(year, start, 1), month_end(year, end), "range", basis)
        start, end = QUARTER_MONTHS[index]
        return _span(date(year, start, 1), month_end(year, end), "quarter", basis)
    return None


def normalize_time(raw: str, published_at: str | None = None) -> dict[str, str] | None:
    """Resolve an event-time mention to an ISO date or interval.

    Returns ``None`` when the mention cannot be resolved, which the caller
    records as ``unresolved`` — §9.3 forbids inventing a value.
    """
    text = strip_decorations((raw or "").strip())
    if not text:
        return None
    published = publication_date(published_at)

    # A range must be split before anything else, or the single-date patterns
    # below would silently return only its left endpoint.
    parts = [part for part in RANGE_SPLIT.split(text) if part.strip()]
    if len(parts) == 2:
        left = normalize_time(parts[0], published_at)
        right = normalize_time(parts[1], published_at)
        # The left endpoint of "1 June to 16 June" carries no year of its own,
        # so it is retried with the right endpoint's year supplied as context.
        if right and not left:
            left = normalize_time(parts[0], _bounds(right)[0].isoformat())
        if left and right:
            basis = "explicit" if {left["basis"], right["basis"]} == {"explicit"} else "relative_to_publication"
            return _span(_bounds(left)[0], _bounds(right)[1], "range", basis)

    explicit_day = re.search(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?", text)
    if explicit_day:
        year, month, day = (int(part) for part in explicit_day.groups())
        try:
            return _span(date(year, month, day), date(year, month, day), "day", "explicit")
        except ValueError:
            return None

    half = re.search(r"(\d{4})?\s*年?\s*(上半年|下半年)", text)
    if half:
        year = int(half.group(1)) if half.group(1) else (published.year if published else None)
        if year is None:
            return None
        start, end = ((1, 6) if half.group(2) == "上半年" else (7, 12))
        return _span(date(year, start, 1), month_end(year, end), "range", "explicit" if half.group(1) else "relative_to_publication")

    quarter = re.search(r"(\d{4})?\s*年?\s*(?:第)?([1-4一二三四])\s*季度|(\d{4})\s*[Qq]([1-4])", text)
    if quarter:
        if quarter.group(4):
            year, index = int(quarter.group(3)), int(quarter.group(4))
            basis = "explicit"
        else:
            index = CN_DIGITS.get(quarter.group(2), None) or int(quarter.group(2))
            year = int(quarter.group(1)) if quarter.group(1) else (published.year if published else None)
            basis = "explicit" if quarter.group(1) else "relative_to_publication"
        if year is None:
            return None
        start, end = QUARTER_MONTHS[index]
        return _span(date(year, start, 1), month_end(year, end), "quarter", basis)

    explicit_month = re.search(r"(\d{4})[年\-/.](\d{1,2})月?(?!\d)", text)
    if explicit_month:
        year, month = int(explicit_month.group(1)), int(explicit_month.group(2))
        if not 1 <= month <= 12:
            return None
        return _span(date(year, month, 1), month_end(year, month), "month", "explicit")

    period = english_period(text, published)
    if period:
        return period

    english = english_date(text, published)
    if english:
        # A year-less English date borrowed the publication year.
        basis = "explicit" if re.search(r"(?<!\d)\d{4}(?!\d)", text) else "relative_to_publication"
        if basis == "relative_to_publication" and not published:
            return None
        return _span(english, english, "day", basis)

    english_month = re.search(rf"\b({MONTH_PATTERN})\b\s*,?\s*(\d{{4}})", text, re.I)
    if english_month:
        month, year = MONTH_NAMES[english_month.group(1).lower()], int(english_month.group(2))
        return _span(date(year, month, 1), month_end(year, month), "month", "explicit")

    # A bare 月/日 borrows the year (and month) from the publication date.
    if published:
        bare_day = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text)
        if bare_day:
            month, day = int(bare_day.group(1)), int(bare_day.group(2))
            try:
                return _span(date(published.year, month, day), date(published.year, month, day), "day", "relative_to_publication")
            except ValueError:
                return None
        relative_year = re.search(r"(今年|去年|上年|本年度|去年同期|前年)", text)
        if relative_year:
            offset = {"今年": 0, "本年度": 0, "去年": -1, "上年": -1, "去年同期": -1, "前年": -2}[relative_year.group(1)]
            year = published.year + offset
            return _span(date(year, 1, 1), date(year, 12, 31), "year", "relative_to_publication")
        if re.search(r"(今日|当日|即日)", text):
            return _span(published, published, "day", "relative_to_publication")
        if re.search(r"(昨日|昨天)", text):
            previous = published - timedelta(days=1)
            return _span(previous, previous, "day", "relative_to_publication")
        # 截至本报告披露日 and "as at the date of this announcement" both name the
        # publication date itself, which is the one relative expression that
        # resolves to an exact day.
        if re.search(r"(截至本?报告(披露|出具)?日|截至本公告(披露)?日)", text) or \
                re.search(r"date of this (announcement|report|circular)", text, re.I):
            return _span(published, published, "day", "relative_to_publication")
        if re.search(r"(近日|日前|近期|最近|不久前)", text) or \
                re.search(r"\b(recently|recent|of late)\b", text, re.I):
            # Deliberately coarse: "近日" locates the event in the reporting
            # month, and claiming day precision here would be a fabrication.
            return _span(date(published.year, published.month, 1),
                         month_end(published.year, published.month), "month", "relative_to_publication")
        bare_month = re.search(r"(?<!\d)(\d{1,2})月[初中底末]?(?!\d)", text)
        if bare_month and 1 <= int(bare_month.group(1)) <= 12:
            month = int(bare_month.group(1))
            return _span(date(published.year, month, 1), month_end(published.year, month),
                         "month", "relative_to_publication")
        # A month name standing alone, the English counterpart of 9月底.  The
        # fullmatch is deliberate: a loose search would read the "may" in
        # ordinary prose as a date.
        lone_month = re.fullmatch(rf"({MONTH_PATTERN})", text.strip(), re.I)
        if lone_month:
            month = MONTH_NAMES[lone_month.group(1).lower()]
            return _span(date(published.year, month, 1), month_end(published.year, month),
                         "month", "relative_to_publication")
        if re.search(r"年[中初末底]", text):
            return _span(date(published.year, 1, 1), date(published.year, 12, 31),
                         "year", "relative_to_publication")
        if re.search(r"\b(today|this\s+morning|this\s+afternoon)\b", text, re.I):
            return _span(published, published, "day", "relative_to_publication")
        if re.search(r"\byesterday\b", text, re.I):
            previous = published - timedelta(days=1)
            return _span(previous, previous, "day", "relative_to_publication")
        if re.search(r"\blast\s+week\b", text, re.I):
            return _span(published - timedelta(days=7), published - timedelta(days=1),
                         "range", "relative_to_publication")
        if re.search(r"\blast\s+month\b", text, re.I):
            end = date(published.year, published.month, 1) - timedelta(days=1)
            return _span(date(end.year, end.month, 1), end, "month", "relative_to_publication")
        if re.search(r"\blast\s+year\b", text, re.I):
            return _span(date(published.year - 1, 1, 1), date(published.year - 1, 12, 31),
                         "year", "relative_to_publication")
        months_ago = re.search(rf"\b({'|'.join(EN_COUNTS)}|\d{{1,2}})\s+months?\s+ago\b", text, re.I)
        if months_ago:
            token = months_ago.group(1).lower()
            offset = EN_COUNTS.get(token) or int(token)
            anchor = window_start(date(published.year, published.month, 1), offset + 1)
            return _span(anchor, month_end(anchor.year, anchor.month), "month", "relative_to_publication")

    explicit_year = re.search(r"(\d{4})\s*年(?!\d)", text)
    if explicit_year:
        year = int(explicit_year.group(1))
        return _span(date(year, 1, 1), date(year, 12, 31), "year", "explicit")

    # A four-digit year as the only number in the mention — "2025", "late 2024",
    # "the full year of 2025".  Requiring it to stand alone keeps this from
    # swallowing a year that belongs to a pattern above.
    lone_year = re.fullmatch(r"\D*(\d{4})\D*", text)
    if lone_year:
        year = int(lone_year.group(1))
        if 1990 <= year <= 2100:
            return _span(date(year, 1, 1), date(year, 12, 31), "year", "explicit")
    return None


def normalize_amount(raw: str) -> dict[str, Any] | None:
    """Resolve a money mention to a base-unit value plus currency."""
    text = (raw or "").strip().lower()
    if not text:
        return None
    # A ratio that drifted into the amounts list is not money; treating it as
    # such would invent a 31 元 figure out of "约31%".
    if "%" in text or "百分之" in text or any(term in text for term in PERCENTAGE_POINT_TERMS):
        return None
    number = re.search(r"(\d+(?:[\d,]*\d)?(?:\.\d+)?)", text)
    if not number:
        return None
    try:
        value = float(number.group(1).replace(",", ""))
    except ValueError:
        return None
    tail = text[number.end():]
    scale = 1
    unit_raw = ""
    for token, multiplier in CN_SCALES.items():
        if token in tail:
            scale, unit_raw = multiplier, token
            break
    else:
        for token, multiplier in EN_SCALES.items():
            if re.search(rf"\b{token}\b", tail):
                scale, unit_raw = multiplier, token
                break
    currency = None
    for token, code in CURRENCIES.items():
        if token in text:
            currency = code
            # 元 also appears inside 美元/港元, so keep scanning for a longer hit.
            if token not in {"元", "$"}:
                break
    return {"value_normalized": round(value * scale, 2), "currency": currency or "CNY",
            "unit_raw": unit_raw or "元", "currency_assumed": currency is None}


# Exact quantities for comparing a claim with a filing.
#
# A figure checked by a model is a figure the model may convert wrongly: the
# adjudicator once read 708.15亿股 as 7,081,500,000 股 — a factor of ten short —
# and reported a 637亿-share conflict between two numbers that differ by 57,468
# shares.  Conversion is arithmetic, so it is done here, in Decimal, and the
# model is handed the result.
#
# 万亿 precedes 万 and 亿 so a trillion is not read as ten thousand.  千 is not a
# scale here: 「3千」 in running text is rarely money, and a bare scale counts.
QUANTITY_SCALES = {"万亿": Decimal(10) ** 12, "十亿": Decimal(10) ** 9, "千万": Decimal(10) ** 7,
                   "百万": Decimal(10) ** 6, "亿": Decimal(10) ** 8, "万": Decimal(10) ** 4}
# Units a figure can be counted in.  人民币 is how the filing writes 元 when it
# puts the currency first; a bare scale with no unit is left unit-less.
QUANTITY_UNITS = {"美元": "美元", "港元": "港元", "港币": "港元", "欧元": "欧元", "日元": "日元",
                  "人民币": "元", "元": "元", "股": "股"}
QUANTITY_PATTERN = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    rf"({'|'.join(QUANTITY_SCALES)})?\s*({'|'.join(sorted(QUANTITY_UNITS, key=len, reverse=True))})?")
PERCENT_PATTERN = re.compile(r"(?<![\d.,])(\d+(?:\.\d+)?)\s*%")
# Two figures a power of ten apart and otherwise equal are a unit slip, not a
# disagreement about the number — the error I-02 was about.  Only a figure with
# enough significant digits can be recognised that way: 50% against 5%, or 300亿
# against 30亿, are as likely two different quantities as one misplaced unit.
MAGNITUDE_SLIP_TOLERANCE = Decimal("0.005")
MAGNITUDE_SLIP_MIN_DIGITS = 3


def significant_digits(digits: str) -> int:
    """``708.15`` → 5, ``3,000`` → 1, ``0.4714`` → 4."""
    plain = digits.replace(",", "")
    if "." not in plain:
        plain = plain.rstrip("0")
    return len(plain.replace(".", "").lstrip("0"))


def extract_quantities(text: str) -> list[dict[str, Any]]:
    """Every counted figure in ``text``, in base units, with its stated precision.

    ``step`` is one unit of the last digit the text wrote: 708.15亿 is stated to
    the nearest 10⁶, so a filing value within half of that is the same figure.
    A number with neither a scale nor a unit — a year, a page, a count of
    people — is not a quantity and is skipped.
    """
    found: list[dict[str, Any]] = []
    for match in QUANTITY_PATTERN.finditer(text or ""):
        digits, scale_token, unit_token = match.groups()
        if not scale_token and not unit_token:
            continue
        try:
            number = Decimal(digits.replace(",", ""))
        except InvalidOperation:
            continue
        scale = QUANTITY_SCALES.get(scale_token or "", Decimal(1))
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        found.append({"raw": match.group(0).strip(), "value": number * scale,
                      "step": scale * Decimal(10) ** -decimals,
                      "digits": significant_digits(digits), "scaled": bool(scale_token),
                      "unit": QUANTITY_UNITS.get(unit_token or "", "")})
    for match in PERCENT_PATTERN.finditer(text or ""):
        digits = match.group(1)
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        found.append({"raw": match.group(0).strip(), "value": Decimal(digits),
                      "step": Decimal(10) ** -decimals, "digits": significant_digits(digits),
                      "scaled": False, "unit": "%"})
    return found


def format_quantity(value: Decimal, unit: str) -> str:
    """``70815057468`` → ``70,815,057,468股``: grouped, no float, no trailing zeros."""
    text = f"{value.normalize():,f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}{unit}"


def relative_text(ratio: Decimal) -> str:
    """A relative difference with two significant digits and no exponent."""
    if ratio == 0:
        return "0%"
    percent = ratio * 100
    places = max(0, 2 - percent.adjusted() - 1)
    return f"{percent:.{places}f}%"


def _units_comparable(left: str, right: str) -> bool:
    if left == right:
        return True
    # A bare 亿 in a headline may be shares or money; it is compared with
    # anything counted, never with a percentage.
    return "" in (left, right) and "%" not in (left, right)


def compare_quantities(claim: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Compare one claim figure with one filing figure, exactly.

    ``agree`` means the filing value rounds to what the claim wrote; ``slip``
    means they match up to a power of ten; anything else is ``disagree`` —
    which says only that *these two* differ, not that the claim is wrong.
    """
    unit = claim["unit"] or evidence["unit"]
    difference = abs(claim["value"] - evidence["value"])
    base = abs(evidence["value"])
    ratio = difference / base if base else (Decimal(0) if not difference else Decimal(1))
    if difference <= claim["step"] / 2:
        status = "agree"
    else:
        status = "disagree"
        if claim["value"] and evidence["value"] and claim["digits"] >= MAGNITUDE_SLIP_MIN_DIGITS:
            scaled = abs(claim["value"] / evidence["value"])
            power = round(scaled.log10())
            if power and abs(scaled / Decimal(10) ** power - 1) <= MAGNITUDE_SLIP_TOLERANCE:
                status = "slip"
    return {"status": status, "claim_raw": claim["raw"], "evidence_raw": evidence["raw"],
            "claim_value": format_quantity(claim["value"], unit),
            "evidence_value": format_quantity(evidence["value"], unit),
            "difference": format_quantity(difference, unit), "relative": relative_text(ratio),
            "_rank": (status != "agree", ratio)}


def numeric_check(claim_text: str, evidence_text: str) -> dict[str, Any]:
    """Check every figure a claim states against the filing, asserting only what is provable.

    A claim figure is paired with a filing figure only when the two agree
    within the claim's precision, or are the same digits a power of ten apart.
    Anything else is *unmatched*, never "disagreeing": the nearest number in an
    excerpt is often a different quantity — 发行前 against 发行后 总股本, a
    registered-capital threshold against a par value — and reporting the gap
    between them as a discrepancy would mislead the reviewer the check is for.

    ``agree``: every figure found and agreeing.  ``slip``: at least one unit
    slip.  ``partial``: some agree, the rest unmatched.  ``none``: nothing paired.
    """
    claim_figures = extract_quantities(claim_text)
    evidence_figures = extract_quantities(evidence_text)
    figures: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for claim in claim_figures:
        options = [result for evidence in evidence_figures
                   if _units_comparable(claim["unit"], evidence["unit"])
                   and (result := compare_quantities(claim, evidence))["status"] != "disagree"]
        if not options:
            unmatched.append(claim["raw"])
            continue
        best = min(options, key=lambda item: item["_rank"])
        best.pop("_rank")
        figures.append(best)
    if not figures:
        status = "none"
    elif any(item["status"] == "slip" for item in figures):
        status = "slip"
    else:
        status = "partial" if unmatched else "agree"
    return {"status": status, "figures": figures, "unmatched": unmatched}


def numeric_check_lines(check: dict[str, Any]) -> list[str]:
    """The check written out for a reader — the model and the reviewer see the same sentences."""
    verdicts = {"agree": "在主张的表述精度内一致", "slip": "相差整 10 的幂，疑似单位换算错误"}
    return [f"主张「{item['claim_raw']}」= {item['claim_value']}；原文「{item['evidence_raw']}」= "
            f"{item['evidence_value']}；差 {item['difference']}（{item['relative']}），{verdicts[item['status']]}"
            for item in check["figures"]]


def normalize_ratio(raw: str) -> dict[str, Any] | None:
    """Resolve a ratio mention, keeping 百分比 and 百分点 apart."""
    text = (raw or "").strip().lower()
    if not text:
        return None
    number = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if not number:
        return None
    ratio_type = "percentage_point" if any(term in text for term in PERCENTAGE_POINT_TERMS) else "percent"
    if ratio_type == "percent" and "%" not in text and "百分之" not in text and "percent" not in text:
        return None
    return {"value": float(number.group(1)), "ratio_type": ratio_type}


def load_entities(config_path: Any = CONFIG_PATH) -> dict[str, dict[str, Any]]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return config.get("entities", {}) or {}


def load_organizations(config_path: Any = ORGANIZATIONS_PATH) -> dict[str, dict[str, Any]]:
    """Third parties that are named in events but are not monitored companies.

    A separate namespace from ``entities`` on purpose: this resolves to an
    ``org_id``, never a ``company_id``.  药明生物 and 药明合联 are listed here
    *and* excluded from 药明康德 in ``sources.yaml`` -- both remain true, and
    the second is what keeps two issuers' ledgers apart.
    """
    if not config_path.exists():
        return {}
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return config.get("organizations", {}) or {}


def _name_forms(record: dict[str, Any]) -> list[str]:
    forms = [str(record.get("canonical_name", ""))]
    forms.extend(str(alias) for alias in record.get("aliases") or [])
    return [form for form in forms if form]


def resolve_organization(name: str, registry: dict[str, dict[str, Any]]) -> tuple[str | None, str | None]:
    """Link a mention to a known third-party organisation, and say how.

    Two rules, in order.  An exact fold-insensitive match on any registered
    name form; failing that, a **prefix** match, which is what collapses the
    branch offices.  A subject reading 中国国际金融股份有限公司北京建国门外大街
    证券营业部 is 中金 with an address attached -- enumerating branches would be
    a losing game, and the corpus already held four spellings of that one firm.

    The prefix needs six characters.  Shorter ones match by accident: 三星 is a
    prefix of 三星堆.
    """
    text = fold((name or "").strip().lower())
    if not text:
        return None, None
    for org_id, record in registry.items():
        for form in _name_forms(record):
            if fold(form.lower()) == text:
                return org_id, "organization_registry"
    # Longest prefix wins, so a firm registered under both its full name and a
    # short one is credited through the more specific of the two.
    best: tuple[str, int] | None = None
    for org_id, record in registry.items():
        for form in _name_forms(record):
            folded = fold(form.lower())
            if len(folded) >= 6 and text.startswith(folded):
                if best is None or len(folded) > best[1]:
                    best = (org_id, len(folded))
    if best is not None:
        return best[0], "organization_branch"
    return None, None


def resolve_subject_with_basis(name: str, entities: dict[str, dict[str, Any]],
                               document_company: str | None = None) -> tuple[str | None, str | None]:
    """``resolve_subject``, plus the reason -- which the elements page shows.

    Two mention shapes reach here that the alias table can never match, and
    both were sitting at the top of the unresolved list:

    * the ``company_id`` itself, 72 occurrences and the most common unresolved
      subject in the corpus.  The labelling pass backfills ``event_subjects``
      with the document's company id when the model returns none, and an id is
      not one of that company's aliases -- so the mention the corpus could least
      afford to lose was **a case company failing to recognise itself**;
    * anaphora.  "the Company" is not a name, it is a pointer to the document
      this element came from, and the document knows its own company.
    """
    text = (name or "").strip()
    if not text:
        return None, None
    if text in entities:
        return text, "company_id_literal"
    if document_company and fold(text.lower()) in ANAPHORA:
        return document_company, "document_company"
    matched = resolve_subject(text, entities)
    return (matched, "alias") if matched else (None, None)


def resolve_subject(name: str, entities: dict[str, dict[str, Any]]) -> str | None:
    """Link a subject mention to a monitored company, honouring the alias rules.

    The exclusion list in ``config/sources.yaml`` exists because names such as
    长江存储 and 药明生物 are separate issuers; matching them here would merge
    two companies' events.

    Matching folds traditional characters to simplified on both sides, for the
    same reason the evidence index does: 藥明康德 in a Hong Kong announcement and
    药明康德 in a mainland report are one company, and comparing the two scripts
    literally left the case company itself unresolved 37 times.  Only the
    comparison is folded — ``raw_text`` keeps the mention as it was written.
    """
    text = (name or "").strip()
    if not text:
        return None
    lowered = fold(text.lower())
    for company_id, entity in entities.items():
        for excluded in entity.get("exclude_or_review", []) or []:
            term = fold(str(excluded.get("term", "")).lower())
            # An excluded term only blocks when it is the whole mention; a
            # 长江存储 mention must not suppress a 长鑫科技 one in the same list.
            if term and lowered == term:
                return None
    for company_id, entity in entities.items():
        aliases = [str(alias) for alias in entity.get("include_aliases", []) or []]
        aliases.append(str(entity.get("canonical_name", "")))
        for alias in aliases:
            if alias and fold(alias.lower()) == lowered:
                return company_id
        for code in entity.get("stock_codes", []) or []:
            if str(code.get("code", "")) and str(code["code"]) in text:
                return company_id
    for company_id, entity in entities.items():
        for alias in entity.get("include_aliases", []) or []:
            if len(str(alias)) >= 4 and fold(str(alias).lower()) in lowered:
                return company_id
    return None


def element_rows(event: sqlite3.Row, entities: dict[str, dict[str, Any]],
                 organizations: dict[str, dict[str, Any]] | None = None) -> list[tuple[Any, ...]]:
    """Build every normalized element row for one event."""
    organizations = organizations or {}
    rows: list[tuple[Any, ...]] = []

    def add(element_type: str, ordinal: int, raw: str, value: Any, unit: str | None,
            basis: str | None, extra: dict[str, Any] | None = None) -> None:
        status = "normalized" if value is not None else "unresolved"
        rows.append((element_type, ordinal, raw, None if value is None else str(value),
                     unit, basis, status, json.dumps(extra or {}, ensure_ascii=False)))

    label = json.loads(event["raw_label_json"] or "{}")
    published_at = event["published_at"]

    raw_time = (label.get("event_time") or "").strip()
    if raw_time:
        resolved = normalize_time(raw_time, published_at)
        add("time", 1, raw_time, resolved["value"] if resolved else None,
            resolved["precision"] if resolved else None,
            resolved["basis"] if resolved else None,
            {"stated_precision": label.get("event_time_precision")})

    for ordinal, subject in enumerate(label.get("event_subjects") or [], start=1):
        if not str(subject).strip():
            continue
        company, basis = resolve_subject_with_basis(str(subject), entities, event["company_id"])
        if company is not None:
            add("subject", ordinal, str(subject), company, "company_id", basis)
            continue
        # Not a monitored company.  Most of these are counterparties,
        # regulators and brokers -- normal, and worth a name rather than a
        # blank.  ``org_id`` keeps them out of the company namespace.
        org, org_basis = resolve_organization(str(subject), organizations)
        add("subject", ordinal, str(subject), org, "org_id" if org else None, org_basis)

    for ordinal, amount in enumerate(label.get("amounts") or [], start=1):
        raw = amount if isinstance(amount, str) else amount.get("raw", "")
        if not str(raw).strip():
            continue
        resolved = normalize_amount(str(raw))
        add("amount", ordinal, str(raw), resolved["value_normalized"] if resolved else None,
            resolved["currency"] if resolved else None, None,
            {"unit_raw": resolved["unit_raw"], "currency_assumed": resolved["currency_assumed"]} if resolved else {})

    for ordinal, ratio in enumerate(label.get("ratios") or [], start=1):
        raw = ratio if isinstance(ratio, str) else ratio.get("raw", "")
        if not str(raw).strip():
            continue
        resolved = normalize_ratio(str(raw))
        add("ratio", ordinal, str(raw), resolved["value"] if resolved else None,
            resolved["ratio_type"] if resolved else None, None)

    for element_type, field in (("person", "persons"), ("organization", "organizations"), ("product", "products")):
        for ordinal, item in enumerate(label.get(field) or [], start=1):
            raw = item if isinstance(item, str) else item.get("name", "")
            if not str(raw).strip():
                continue
            if element_type == "person":
                match = re.match(r"^\s*([^（(]+)[（(]([^）)]*)[）)]\s*$", str(raw))
                name = match.group(1).strip() if match else str(raw).strip()
                role = match.group(2).strip() if match else None
                add("person", ordinal, str(raw), name, "name", None, {"role": role})
            else:
                add(element_type, ordinal, str(raw), str(raw).strip(), None, None)
    return rows


def run(companies: set[str] | None = None) -> dict[str, int]:
    db = connect()
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    entities = load_entities()
    organizations = load_organizations()
    query = """SELECT e.event_id,e.company_id,e.raw_label_json,v.published_at
               FROM event_records e JOIN document_versions v ON v.version_id=e.version_id"""
    params: list[str] = []
    if companies:
        query += " WHERE e.company_id IN (%s)" % ",".join("?" * len(companies))
        params = sorted(companies)
    events = db.execute(query, params).fetchall()
    # Elements outlive an event that a later run downgraded to NO_EVENT, and the
    # per-event DELETE below never reaches them because their event is no longer
    # in the result set.
    orphaned = db.execute("""DELETE FROM event_elements WHERE event_id NOT IN
        (SELECT event_id FROM event_records)""").rowcount
    counts = {"events": len(events), "elements": 0, "unresolved": 0, "event_time_set": 0,
              "needs_review": 0, "orphaned_removed": max(orphaned, 0)}
    for event in events:
        db.execute("DELETE FROM event_elements WHERE event_id=?", (event["event_id"],))
        # A label that mentioned no time at all leaves an empty string behind
        # from the labelling insert.  Normalizing it to NULL keeps the column
        # one thing — an ISO value or nothing — for every consumer.
        db.execute("UPDATE event_records SET event_time=NULL WHERE event_id=? AND event_time=''",
                   (event["event_id"],))
        resolved_time = None
        for element_type, ordinal, raw, value, unit, basis, status, extra in element_rows(
                event, entities, organizations):
            element_id = hashlib.sha256(f"{event['event_id']}\0{element_type}\0{ordinal}".encode()).hexdigest()
            db.execute("""INSERT INTO event_elements
                (element_id,event_id,element_type,ordinal,raw_text,normalized_value,unit,basis,status,extra_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (element_id, event["event_id"], element_type, ordinal, raw, value, unit, basis, status, extra))
            counts["elements"] += 1
            counts["unresolved"] += int(status == "unresolved")
            if element_type == "time":
                # event_records keeps the resolved value so timelines and exports
                # can sort without joining the element table.  An unresolved
                # mention clears the column instead of leaving the model's raw
                # words in it: "截至11:00" sorts lexically against ISO dates and
                # makes every range query on this column quietly wrong.  The
                # wording is not lost — it stays in raw_text and raw_label_json.
                db.execute("UPDATE event_records SET event_time=?,event_time_precision=?,updated_at=? WHERE event_id=?",
                           (value, unit or "unknown", utc_now(), event["event_id"]))
                resolved_time = value
                counts["event_time_set"] += int(status == "normalized")
        # Written on every pass, so a time that stops resolving also stops being
        # called a plan, and old rows acquire the role without a migration.
        db.execute("UPDATE event_records SET event_time_role=? WHERE event_id=?",
                   (event_time_role(resolved_time, event["published_at"]), event["event_id"]))
        # A stated-but-unresolvable time or quantity is the case §9.3 sends to
        # review.  An unresolved subject usually just means a third party that is
        # not on the monitoring list, which is normal and not a defect.
        blocked = db.execute("""SELECT 1 FROM event_elements WHERE event_id=? AND status='unresolved'
            AND element_type IN ('time','amount','ratio') LIMIT 1""", (event["event_id"],)).fetchone()
        # Passing the outcome either way is the point: an event whose date this
        # pass just resolved must come back out of the review queue.
        set_review_reason(db, event["event_id"], "unresolved_elements", blocked is not None)
        counts["needs_review"] += int(blocked is not None)
    db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "event_elements_normalized", **counts})
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Standardize extracted event elements offline")
    parser.add_argument("--company", action="append", choices=configured_companies())
    args = parser.parse_args()
    print(json.dumps(run(set(args.company) if args.company else None), ensure_ascii=False))


if __name__ == "__main__":
    main()
