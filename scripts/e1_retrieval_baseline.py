#!/usr/bin/env python3
"""What E1 scores with no model at all, and where the model loses to it.

Adding sixteen retrieved examples to E1's prompt lifted test micro-F1 from
0.4211 to 0.6497.  That is a large number and it would have been reported as a
prompt-engineering win, except for the control this script runs: **copy the
nearest labelled article's answer and skip the model entirely.**  That scores
0.6685 micro and 0.6318 macro.  The gain is retrieval; the model is a net loss
on top of it.

Two things could make that comparison a lie, so both are measured here rather
than argued:

  * **Twins.**  The gold contains reprints of one story, so a nearest neighbour
    might be the same article wearing a different byline, and copying its label
    would be recall of a duplicate rather than generalisation.  The strata below
    separate them and report the share.
  * **The prior.**  A vocabulary this skewed can be gamed by always answering
    TECH_LEVEL, so the majority-class baseline is printed beside every number.

Similarity is measured by **containment**, not Jaccard: the query is a whole
article and an indexed example is a 220-character snippet, and Jaccard divides
by the union, so it reads every pair as unrelated purely because one side is
short.  That is the same defect 交付物7 §7bis was written about, met a second
time in the space of a day.

Read-only.  Touches the benchmark database only, and calls no model.
The retrieval-example quality comparison is summarized in
docs/submission/05-测试报告.md; this script reproduces the detailed experiments.
"""
from __future__ import annotations

import argparse
import collections
import statistics

from processor import gold_examples
from processor.benchmark import (BENCHMARK_DB, SPLITS, connect, load_taxonomy_map,
                                 multilabel_scores)
from processor.issue_codes import candidates, sample
from processor.pipeline import tokenize

# Where the nearest neighbour stops being "the same story" and starts being
# "another story about the same thing".  Reported as strata rather than used as
# a gate, so the reader can see the share each band carries.
TWIN = 0.6
CLOSE = 0.3


def containment(left: str, right: str) -> float:
    first, second = tokenize(left), tokenize(right)
    smaller = min(len(first), len(second))
    return len(first & second) / smaller if smaller else 0.0


def evaluate(db, split: str, per_code_cap: int, neighbours: int) -> None:
    rows = sample(candidates(db, split, load_taxonomy_map()), per_code_cap)
    overall, strata, scores = [], collections.defaultdict(list), []
    for _, text, row, truth in rows:
        found = gold_examples.examples_for(
            db, gold_examples.query_text(row["title"], text), neighbours, "issue_codes",
            exclude_article_id=row["article_file_id"])
        said = set(found[0]["issue_codes"]) if found else set()
        overall.append((said, truth))
        if not found:
            strata["④ 无召回"].append((said, truth))
            continue
        overlap = containment(f"{row['title'] or ''}\n{(text or '')[:2000]}",
                              f"{found[0]['title'] or ''}\n{found[0]['body']}")
        scores.append(overlap)
        band = (f"① 高度重合 c>={TWIN}" if overlap >= TWIN
                else f"② 相近 {CLOSE}-{TWIN}" if overlap >= CLOSE
                else f"③ 弱相关 c<{CLOSE}")
        strata[band].append((said, truth))

    counts = collections.Counter(code for _, truth in overall for code in truth)
    frequent = min(counts, key=lambda code: (-counts[code], code)) if counts else None
    prior = multilabel_scores([({frequent}, truth) for _, truth in overall]) if frequent else None
    result = multilabel_scores(overall)

    print(f"split={split}  样本 {len(rows)} 篇  每码封顶 {per_code_cap}")
    if scores:
        print(f"top-1 包含度：中位 {statistics.median(scores):.3f}  "
              f"p90 {sorted(scores)[int(len(scores) * 0.9)]:.3f}")
    print(f"\n{'方法':<28}{'micro-F1':>11}{'macro-F1':>11}{'exact':>9}")
    if prior:
        print(f"{('多数类基线（' + frequent + '）'):<28}"
              f"{prior['micro_f1']:>11.4f}{prior['macro_f1']:>11.4f}{prior['exact_set_match']:>9.4f}")
    print(f"{'纯 1-NN 检索（不调模型）':<28}"
          f"{result['micro_f1']:>11.4f}{result['macro_f1']:>11.4f}{result['exact_set_match']:>9.4f}")

    print(f"\n{'分层':<24}{'篇数':>6}{'占比':>8}{'micro-F1':>11}")
    for band in sorted(strata):
        pairs = strata[band]
        micro = multilabel_scores(pairs)["micro_f1"] if pairs else 0.0
        print(f"{band:<24}{len(pairs):>6}{len(pairs) / len(rows) * 100:>7.1f}%{micro:>11.4f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=SPLITS, default="test")
    parser.add_argument("--per-code-cap", type=int, default=100,
                        help="与被对照的模型运行取同一批样本")
    parser.add_argument("--neighbours", type=int, default=1)
    args = parser.parse_args(argv)
    db = connect(BENCHMARK_DB)
    evaluate(db, args.split, args.per_code_cap, args.neighbours)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
