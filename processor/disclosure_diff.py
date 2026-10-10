#!/usr/bin/env python3
"""Compare successive versions of a company's statutory disclosure (主轴 A).

The insight this implements: **content that changed after a regulator's inquiry
is content the regulator already flagged as worth attention.** A prospectus moves
申报稿 → 上会稿 → 注册稿, and each edit corresponds to a question asked and an
answer given.  That signal is public, structured and almost never exploited.

The comparison is deliberately deterministic — no model is involved — so its
output is reproducible and can be audited line by line.  Every difference keeps
the page number on both sides, which is what lets a media claim be adjudicated
against a specific paragraph rather than against a vague impression.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from difflib import SequenceMatcher
from typing import Any, Iterable

from collector.collect import ROOT, audit, connect
from processor.pipeline import configured_companies, ensure_schema, utc_now

# Ordered by disclosure stage, which is also the order the documents are diffed in.
STAGE_ORDER = ("申报稿", "更新申报稿", "上会稿", "注册稿", "招股意向书")
SECTION_PATTERN = re.compile(r"^\s*(第[一二三四五六七八九十百]+[节章]\s*[^\n]{0,40})\s*$")
NAMED_SECTIONS = ("重大事项提示", "释义", "声明", "风险因素", "目录")
PAGE_PATTERN = re.compile(r"^\[PAGE (\d+)\]$")
NUMBER_PATTERN = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
# Units that mark a figure as a date, an ordinal or a document number rather than
# an amount.  A reporting period rolling from 截至2025年6月末 to 12月末 is a real
# edit, but it belongs in the paragraph-level diff, not in a table of 科目 values.
NON_FINANCIAL_SUFFIX = ("年", "月", "日", "号", "页", "届")
# 第X号/第X页/第X条 are references; the figure is an index, not a measurement.
ORDINAL_PREFIX = "第"
# A metric name is the words immediately before a figure.  Only the trailing run
# of words counts: everything before the last digit or punctuation belongs to a
# previous clause or, in a run-together table row, to a previous cell.
METRIC_WINDOW = 24
METRIC_TAIL = re.compile(r"([\u4e00-\u9fff]|[A-Za-z])[\u4e00-\u9fffA-Za-z]*$")
# Trailing copulas carry no meaning once the name is isolated.
METRIC_TRAILING_COPULA = ("为", "是", "达", "计为", "约为", "达到")
MIN_METRIC_CHARS = 2
# A name made only of connectives names nothing: 「增长至2029年的2045亿美元」 leaves
# 年的 behind, which is grammar, not a 科目.
METRIC_FUNCTION_WORDS = set("年月日的约为至和与及或者之其上下前后期内共计增减以")
# A unit names what the figure is counted in, not what it measures.
METRIC_UNIT_ONLY = frozenset({"万元", "亿元", "元", "美元", "港元", "股", "人", "件", "个", "项"})
# A table row whose cells ran together produces one impossibly long token; the
# digits are several values concatenated, so the figure is not a measurement.
MAX_FIGURE_DIGITS = 15
# A list ordinal at the head of a paragraph — 「7、」「（3）」 — renumbers when a
# neighbouring item is inserted, which is not a disclosure change.
LEADING_ORDINAL = re.compile(r"^[（(]?(\d+)[、）).．]")
# 「（25）第S00751号」 — a figure inside brackets is a reference, not a measurement.
BRACKETED = re.compile(r"[（(]\d+[）)]")
# The dates a paragraph states its figures as of.  When these differ between two
# versions the figures moved because the period moved, which is a different kind
# of change from a restatement under the same period.
PERIOD_ANCHOR = re.compile(
    r"20\d{2}年\d{1,2}月\d{1,2}日|20\d{2}财年|20\d{2}[-–—~～至]20\d{2}年|20\d{2}年\d{1,2}月末?")
# A dotted leader followed by a page number is a contents entry, not disclosure.
TOC_PATTERN = re.compile(r"(\.{6,}|·{6,}|…{3,})\s*\d*\s*$")
# A paragraph ends at a sentence terminator; anything else is a wrapped line.
PARAGRAPH_END = "。！？；!?;：:"
STRUCTURE_START = re.compile(
    r"^\s*(（[一二三四五六七八九十百]+）|\([0-9]+\)|[0-9]+[、．.]|[一二三四五六七八九十]+、"
    r"|第[一二三四五六七八九十百]+[节章条]|注[：:]|表\s*\d|附[表注])")
# Paragraphs shorter than this are headers, page furniture or list bullets whose
# differences are noise rather than disclosure changes.
MIN_PARAGRAPH_CHARS = 30
# Above this, two paragraphs are the same paragraph edited; below, they are
# different paragraphs that happened to be adjacent.
REWRITE_SIMILARITY = 0.60


def normalize_paragraph(text: str) -> str:
    """Collapse formatting so only substantive edits register as differences."""
    return re.sub(r"[\s　]+", "", text).replace(",", "").replace("，", "，")


def line_skeleton(line: str) -> str:
    """Strip digits so a footer reads the same on every page."""
    return re.sub(r"\d+", "#", line)


def running_lines(lines: list[tuple[int, str]], share: float = 0.3) -> set[str]:
    """Find the header/footer skeletons repeated across a document's pages."""
    pages_by_skeleton: dict[str, set[int]] = {}
    pages: set[int] = set()
    for page, line in lines:
        pages.add(page)
        pages_by_skeleton.setdefault(line_skeleton(line), set()).add(page)
    if len(pages) < 4:
        return set()
    limit = max(3, len(pages) * share)
    return {skeleton for skeleton, seen in pages_by_skeleton.items() if len(seen) >= limit}


OPENING_MARKS = "（(【[「『《〈“‘"
CLOSING_MARKS = "）)】]」』》〉”’，。、；：？！%"


def is_cjk(character: str) -> bool:
    """Han, plus the fullwidth punctuation that ends and opens Chinese clauses."""
    code = ord(character)
    return (0x3000 <= code <= 0x303F or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFF65)


def join_gap(previous: str, line: str) -> str:
    """What to put between two rejoined lines: nothing, or one space.

    Joining with nothing is right for Chinese, which wraps without a space, and
    that is the case this was written for.  It is wrong for everything else,
    because the separator *was* there and ``.strip()`` had already removed it:
    a line reading ``"subject to the ownership reporting "`` is stripped to
    ``…reporting`` and then welded onto ``requirements…``, producing
    ``reportingrequirements`` — a word in neither line, indexed and searched as
    if it were one.

    The same welding runs numbers together where PyMuPDF emits one line per
    table cell: 610,000.00 followed by 231,920.20 becomes 610,000.00231,920.20.
    Over 19 filings that is 704 paragraphs of 15,833 carrying a figure that
    appears in no table, and they are what a reviewer reads on the evidence
    pane.  Closing the gap only between CJK characters takes those 704 to zero.

    (A first attempt at counting this used a looser pattern and reported 5,236
    paragraphs corpus-wide.  It was matching CFR citations — ``§ 1.2112``,
    ``2.1033(b)(3)`` — in the English filings, which are not fusions at all.
    The figure above is from a pattern those cannot match.)

    So the gap is closed only when both sides are CJK, which is the one case
    where the source had no separator either.  A genuine figure is untouched:
    1,062,225.9999 万股 arrives whole on one line and never reaches here.
    Diff keys are unaffected — ``normalize_paragraph`` strips whitespace before
    comparing — so the stored version comparisons stay valid.

    What this does **not** fix is a table of Chinese cells — 发行股票类型 above
    人民币普通股 still welds, and no rule reading line text alone can tell that
    from a wrapped sentence.  That needs the table geometry from the extractor,
    which is a different job.  ``metric_name`` keeps its workaround for the
    fusions this cannot see.
    """
    if not previous or not line:
        return ""
    # Never separate across a bracket edge.  A line breaking right after 「（」
    # had no space there either, and 「（ A 股）」 is the same cosmetic damage
    # this is meant to remove, arriving from the other direction.
    if previous[-1] in OPENING_MARKS or line[0] in CLOSING_MARKS:
        return ""
    return "" if is_cjk(previous[-1]) and is_cjk(line[0]) else " "


def paragraphs(text: str) -> list[dict[str, Any]]:
    """Split parsed text into paragraphs tagged with their page and section.

    PDF extraction yields one line per rendered line, so a paragraph must be
    rejoined before comparison.  Without this, repagination alone makes every
    wrapped sentence look like an edit: the same sentence broken after a
    different character reads as a modified paragraph.
    """
    lines: list[tuple[int, str]] = []
    page = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        page_marker = PAGE_PATTERN.match(line)
        if page_marker:
            page = int(page_marker.group(1))
            continue
        lines.append((page, line))
    running = running_lines(lines)

    page = 0
    section = "(前言)"
    found: list[dict[str, Any]] = []
    for page, line in lines:
        if line_skeleton(line) in running:
            # A running header or footer.  Its embedded page number changes with
            # every repagination, which would otherwise dominate the diff.
            continue
        heading = SECTION_PATTERN.match(line)
        if heading:
            section = re.sub(r"\s+", " ", heading.group(1)).strip()
            continue
        if line in NAMED_SECTIONS:
            section = line
            continue
        if TOC_PATTERN.search(line):
            # A table-of-contents entry; its trailing page number shifts on every
            # repagination and says nothing about the disclosure itself.
            continue
        previous = found[-1] if found else None
        if (previous is not None and previous["section"] == section
                and not previous["text"].endswith(tuple(PARAGRAPH_END))
                and not STRUCTURE_START.match(line)):
            # Continuation of the previous line; the paragraph keeps the page it
            # started on so evidence still cites where a reader would look.
            previous["text"] += join_gap(previous["text"], line) + line
            previous["key"] = normalize_paragraph(previous["text"])
            continue
        found.append({"page": page, "section": section, "text": line,
                      "key": normalize_paragraph(line)})
    return [item for item in found if len(item["text"]) >= MIN_PARAGRAPH_CHARS]


def numeric_skeleton(text: str) -> str:
    return NUMBER_PATTERN.sub("§", normalize_paragraph(text))


def metric_name(window: str) -> str | None:
    """The 科目 a figure belongs to, or ``None`` when the text does not name one.

    The window is raw prose ending where the figure begins, so it routinely ends
    mid-clause or, inside a run-together table row, mid-number.  Taking only the
    trailing word run is what separates 「……251.11亿美元，净利润」 (which names
    净利润) from 「资产总额30287397.1827159883.」 (which names nothing usable).
    """
    match = METRIC_TAIL.search(window)
    if not match:
        return None
    name = match.group(0)
    for copula in sorted(METRIC_TRAILING_COPULA, key=len, reverse=True):
        if name.endswith(copula) and len(name) > len(copula) + 1:
            name = name[: -len(copula)]
            break
    if len(name) < MIN_METRIC_CHARS:
        return None
    if name in METRIC_UNIT_ONLY or all(char in METRIC_FUNCTION_WORDS for char in name):
        return None
    return name


def is_financial_figure(collapsed: str, start: int, end: int) -> bool:
    """Whether a figure is a measurement rather than a date or a reference.

    Filtering here rather than at display time keeps the difference table honest
    about its own size: a reporting-period rollover still surfaces, but as the
    paragraph edit it is, because a paragraph whose only change is filtered out
    falls through to ``modified``.
    """
    suffix = collapsed[end:end + 1]
    if suffix in NON_FINANCIAL_SUFFIX:
        return False
    # 德师报（审）字（25）第S00751号 — the 号 trails the figure past a letter.
    if "号" in collapsed[end:end + 3]:
        return False
    if collapsed[max(0, start - 1):start] == ORDINAL_PREFIX:
        return False
    if (match := LEADING_ORDINAL.match(collapsed)) and match.start(1) == start:
        return False
    if any(bracket.start() < start and bracket.end() > end
           for bracket in BRACKETED.finditer(collapsed)):
        return False
    token = collapsed[start:end].lstrip("-")
    digits = token.replace(".", "")
    if len(digits) > MAX_FIGURE_DIGITS:
        return False
    # 「…207052.99」 split across a run-together row leaves fragments such as 00
    # and 0047, which read as values but are the tail of a longer number.
    if len(token) > 1 and token[0] == "0" and not token.startswith("0."):
        return False
    return True


def period_anchors(text: str) -> set[str]:
    """The reporting periods a paragraph states its figures as of."""
    return set(PERIOD_ANCHOR.findall(normalize_paragraph(text)))


def is_period_rollover(left: str, right: str) -> bool:
    """Whether the two versions state their figures as of different periods.

    A prospectus rolls 截至2025年6月30日 to 截至2025年12月31日 between drafts and
    every 时点 figure moves with it.  Those movements are real, but they are not
    the same kind of fact as a figure restated under an unchanged period, and a
    difference table that cannot tell them apart invites the reader to treat
    routine updating as a regulator-prompted correction.
    """
    return bool(period_anchors(left) != period_anchors(right))


def numeric_changes(left: str, right: str) -> list[dict[str, Any]]:
    """Pair up figures when two paragraphs differ only in their numbers.

    Requiring identical surrounding wording is what makes the pairing safe: the
    n-th number on the left is then unambiguously the n-th on the right, giving a
    科目级 difference rather than a guess.
    """
    if numeric_skeleton(left) != numeric_skeleton(right):
        return []
    left_numbers = NUMBER_PATTERN.findall(normalize_paragraph(left))
    right_numbers = NUMBER_PATTERN.findall(normalize_paragraph(right))
    if len(left_numbers) != len(right_numbers):
        return []
    collapsed = normalize_paragraph(left)
    changes: list[dict[str, Any]] = []
    for index, match in enumerate(NUMBER_PATTERN.finditer(collapsed)):
        old_raw, new_raw = left_numbers[index], right_numbers[index]
        if old_raw == new_raw:
            continue
        try:
            old_value = float(old_raw.replace(",", ""))
            new_value = float(new_raw.replace(",", ""))
        except ValueError:
            continue
        if not is_financial_figure(collapsed, match.start(), match.end()):
            continue
        # The words immediately before a figure name it in Chinese financial prose.
        window = collapsed[max(0, match.start() - METRIC_WINDOW):match.start()].strip("：:、，,。 ")
        metric = metric_name(window)
        delta = round(new_value - old_value, 4)
        changes.append({
            "metric": metric, "old_value": old_raw, "new_value": new_raw,
            "delta": delta,
            "delta_ratio": round(delta / old_value, 6) if old_value else None,
        })
    return changes


def align_sections(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[str]:
    """Sections present in both versions, in the order they appear on the right."""
    left_sections = {item["section"] for item in left}
    ordered: list[str] = []
    for item in right:
        if item["section"] in left_sections and item["section"] not in ordered:
            ordered.append(item["section"])
    return ordered


def diff_section(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """Yield paragraph-level differences within one aligned section."""
    matcher = SequenceMatcher(None, [item["key"] for item in left], [item["key"] for item in right], autojunk=False)
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            for item in right[right_start:right_end]:
                yield {"diff_kind": "added", "left_text": None, "right_text": item["text"],
                       "left_page": None, "right_page": item["page"], "similarity": None}
            continue
        if tag == "delete":
            for item in left[left_start:left_end]:
                yield {"diff_kind": "removed", "left_text": item["text"], "right_text": None,
                       "left_page": item["page"], "right_page": None, "similarity": None}
            continue
        removed = list(left[left_start:left_end])
        added = list(right[right_start:right_end])
        paired: set[int] = set()
        for old in removed:
            best_index, best_score = None, 0.0
            for index, new in enumerate(added):
                if index in paired:
                    continue
                score = SequenceMatcher(None, old["key"], new["key"], autojunk=False).ratio()
                if score > best_score:
                    best_index, best_score = index, score
            if best_index is not None and best_score >= REWRITE_SIMILARITY:
                paired.add(best_index)
                new = added[best_index]
                changes = numeric_changes(old["text"], new["text"])
                rollover = int(is_period_rollover(old["text"], new["text"]))
                if changes:
                    for change in changes:
                        yield {"diff_kind": "numeric", "left_text": old["text"], "right_text": new["text"],
                               "left_page": old["page"], "right_page": new["page"],
                               "similarity": round(best_score, 4), "period_rollover": rollover, **change}
                else:
                    yield {"diff_kind": "modified", "left_text": old["text"], "right_text": new["text"],
                           "left_page": old["page"], "right_page": new["page"],
                           "similarity": round(best_score, 4), "period_rollover": rollover}
            else:
                yield {"diff_kind": "removed", "left_text": old["text"], "right_text": None,
                       "left_page": old["page"], "right_page": None, "similarity": None}
        for index, new in enumerate(added):
            if index not in paired:
                yield {"diff_kind": "added", "left_text": None, "right_text": new["text"],
                       "left_page": None, "right_page": new["page"], "similarity": None}


def stage_rank(label: str | None) -> int:
    """Where a filing sits in the review sequence, by the stage named in its title.

    Longest name first, because the stages nest: "申报稿" is a substring of
    "更新申报稿", so scanning in sequence order ranks an updated draft as an
    original one.  That went unnoticed while the updated draft was an HTML
    mirror the comparison dropped for having no page anchors; once the exchange
    original arrived the two drafts sorted only by luck of their dates.
    """
    text = label or ""
    for stage in sorted(STAGE_ORDER, key=len, reverse=True):
        if stage in text:
            return STAGE_ORDER.index(stage)
    return len(STAGE_ORDER)


def load_versions(db: sqlite3.Connection, company_id: str, document_type: str,
                  paginated_only: bool = True) -> tuple[list[sqlite3.Row], list[str]]:
    """Return comparable versions plus the titles deliberately left out.

    Comparing a PDF against an HTML mirror of the same filing reports thousands
    of differences that are purely paragraph-splitting artifacts of two parsers.
    Restricting the chain to paginated originals keeps every reported difference
    a real disclosure change — and keeps page anchors available on both sides,
    which the evidence citation depends on.
    """
    rows = db.execute("""SELECT v.version_id,v.title,v.published_at,p.text_path,p.page_count
        FROM document_versions v JOIN processed_documents p ON p.version_id=v.version_id
        WHERE v.company_id=? AND v.document_type=? AND p.extraction_status='ok' AND p.text_path IS NOT NULL""",
        (company_id, document_type)).fetchall()
    kept, skipped = [], []
    for row in rows:
        if paginated_only and row["page_count"] is None:
            skipped.append(str(row["title"]))
        else:
            kept.append(row)
    return sorted(kept, key=lambda row: (stage_rank(row["title"]), str(row["published_at"] or ""))), skipped


def drop_stale_diffs(db: sqlite3.Connection, company_id: str, document_type: str) -> int:
    """Delete every stored diff between two versions of this company's document type."""
    version_ids = [row[0] for row in db.execute(
        "SELECT version_id FROM document_versions WHERE company_id=? AND document_type=?",
        (company_id, document_type))]
    if not version_ids:
        return 0
    placeholders = ",".join("?" for _ in version_ids)
    cursor = db.execute(
        f"""DELETE FROM disclosure_diffs WHERE company_id=?
            AND (left_version_id IN ({placeholders}) OR right_version_id IN ({placeholders}))""",
        (company_id, *version_ids, *version_ids))
    return cursor.rowcount


def compare(db: sqlite3.Connection, company_id: str, left: sqlite3.Row, right: sqlite3.Row) -> int:
    left_paragraphs = paragraphs((ROOT / left["text_path"]).read_text(encoding="utf-8"))
    right_paragraphs = paragraphs((ROOT / right["text_path"]).read_text(encoding="utf-8"))
    pair_key = f"{left['title']}→{right['title']}"
    db.execute("DELETE FROM disclosure_diffs WHERE company_id=? AND pair_key=?", (company_id, pair_key))
    written = 0
    for section in align_sections(left_paragraphs, right_paragraphs):
        left_section = [item for item in left_paragraphs if item["section"] == section]
        right_section = [item for item in right_paragraphs if item["section"] == section]
        for ordinal, difference in enumerate(diff_section(left_section, right_section)):
            diff_id = hashlib.sha256(
                f"{company_id}\0{pair_key}\0{section}\0{ordinal}\0{difference['diff_kind']}".encode()).hexdigest()
            db.execute("""INSERT INTO disclosure_diffs
                (diff_id,company_id,pair_key,left_version_id,right_version_id,left_label,right_label,section,
                 diff_kind,left_text,right_text,left_page,right_page,metric,old_value,new_value,delta,
                 delta_ratio,similarity,period_rollover,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (diff_id, company_id, pair_key, left["version_id"], right["version_id"],
                 left["title"], right["title"], section, difference["diff_kind"],
                 difference.get("left_text"), difference.get("right_text"),
                 difference.get("left_page"), difference.get("right_page"),
                 difference.get("metric"), difference.get("old_value"), difference.get("new_value"),
                 difference.get("delta"), difference.get("delta_ratio"), difference.get("similarity"),
                 difference.get("period_rollover", 0), utc_now()))
            written += 1
    return written


def run(companies: set[str] | None = None, document_type: str = "prospectus",
        paginated_only: bool = True) -> dict[str, Any]:
    db = connect()
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    targets = sorted(companies) if companies else [
        row[0] for row in db.execute("SELECT DISTINCT company_id FROM document_versions WHERE company_id IS NOT NULL")]
    summary: dict[str, Any] = {"document_type": document_type, "pairs": [], "diffs": 0, "skipped": [],
                               "stale_removed": 0}
    for company_id in targets:
        versions, skipped = load_versions(db, company_id, document_type, paginated_only)
        summary["skipped"].extend(f"{company_id}:{title}" for title in skipped)
        # compare() only clears the pair keys it is about to write, so pairs produced by an
        # earlier, wider chain would survive as stale rows. Make this run authoritative for
        # the company and document type by dropping every diff touching one of its versions.
        summary["stale_removed"] += drop_stale_diffs(db, company_id, document_type)
        for left, right in zip(versions, versions[1:]):
            written = compare(db, company_id, left, right)
            summary["pairs"].append({"company": company_id, "pair": f"{left['title']}→{right['title']}",
                                     "diffs": written})
            summary["diffs"] += written
        db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "disclosure_diff_completed",
           "document_type": document_type, "diffs": summary["diffs"]})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare successive disclosure versions offline")
    parser.add_argument("--company", action="append", choices=configured_companies())
    parser.add_argument("--document-type", default="prospectus",
                        help="document type to compare across versions (default: prospectus)")
    parser.add_argument("--include-non-paginated", action="store_true",
                        help="also compare versions parsed from HTML mirrors (reports parser noise)")
    args = parser.parse_args()
    print(json.dumps(run(set(args.company) if args.company else None, args.document_type,
                         paginated_only=not args.include_non_paginated), ensure_ascii=False))


if __name__ == "__main__":
    main()
