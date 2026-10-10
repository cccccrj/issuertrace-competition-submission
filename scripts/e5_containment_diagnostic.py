#!/usr/bin/env python3
"""What the title-containment route recovers, and what still cannot be reached.

Companion to ``e5_band_diagnostic.py``. The method is summarized in
docs/submission/04-模型与算法.md;
this script reproduces containment diagnostics on the gold-labelled data.

That script answered "how high could recall go if the bands were the only
constraint" and its answer was 0.9101 on train.  This one exists because that
answer turned out to be beside the point: brute-forcing every same-entity pair
with no banding and no hamming ceiling recovers *four* additional true pairs.
The pairs the gate misses are not pairs it fails to reach, they are pairs whose
token Jaccard is genuinely low -- because Jaccard divides by the union, so a
brief and the long article that swallows it whole score near zero while one
fully contains the other.

So it reports three things the sweep cannot:

  * where each missed pair actually dies -- banding, hamming, or the bar --
    and what a brute-force comparison would and would not rescue;
  * Jaccard against containment on exactly those pairs, with the length ratio
    that explains the gap;
  * the recall ceiling under *both* candidate routes, which is what replaces
    the 0.9101 of §7.3.

Read-only.  Touches the benchmark database only, never the delivery corpus.
Defaults to the train split: the ceiling in §7bis.6 is a train number on
purpose, because recomputing it on test would be a second look at a split the
calibration is only allowed to spend once.
"""
from __future__ import annotations

import argparse
import collections
import statistics

from processor import pipeline
from processor.benchmark import (SPLITS, connect, gold_clusters, gold_documents,
                                 _pairs)


def prepare(candidates):
    """Everything a gate consults, computed once per document."""
    band_width = 64 // pipeline.SIMHASH_BANDS
    mask = (1 << band_width) - 1
    out = {}
    for doc, text in candidates:
        tokens = pipeline.tokenize(f"{doc.title or ''}\n{text[:12000]}")
        signature = pipeline.simhash(tokens)
        out[doc.version_id] = {
            "doc": doc, "tokens": tokens, "signature": signature,
            "bands": frozenset((band, (signature >> (band * band_width)) & mask)
                               for band in range(pipeline.SIMHASH_BANDS)),
            "title": pipeline.normalize_title(doc.title),
        }
    return out


def measure(left, right):
    overlap = len(left["tokens"] & right["tokens"])
    union = len(left["tokens"] | right["tokens"])
    smaller = min(len(left["tokens"]), len(right["tokens"]))
    larger = max(len(left["tokens"]), len(right["tokens"]))
    return {
        "jaccard": overlap / union if union else 0.0,
        "containment": overlap / smaller if smaller else 0.0,
        "length_ratio": larger / smaller if smaller else 0.0,
        "hamming": pipeline.hamming(left["signature"], right["signature"]),
        "banded": bool(left["bands"] & right["bands"]),
        "same_title": bool(left["title"]) and left["title"] == right["title"],
        "contemporaneous": pipeline.within_days(
            left["doc"].published_at, right["doc"].published_at, pipeline.REPRINT_GAP_DAYS),
    }


def report(db, split: str) -> None:
    candidates, version_to_article, excluded = gold_documents(db, split)
    by_article = {article: version for version, article in version_to_article.items()}
    gold = _pairs(gold_clusters(db, split), set(version_to_article.values()))
    info = prepare(candidates)

    stage = collections.Counter()
    below_bar, reachable_by = [], collections.Counter()
    for pair in gold:
        left, right = (info[by_article[article]] for article in sorted(pair))
        if left["doc"].company_id != right["doc"].company_id:
            stage["cross_entity"] += 1
            reachable_by["neither"] += 1
            continue
        m = measure(left, right)
        bar = (pipeline.TITLE_MATCH_THRESHOLD if m["same_title"]
               else pipeline.BODY_ONLY_THRESHOLD)
        if not m["banded"]:
            stage["no_band_collision"] += 1
        elif m["hamming"] > pipeline.SIMHASH_HAMMING_MAX:
            stage["hamming_over_ceiling"] += 1
        elif m["jaccard"] < bar:
            stage["below_similarity_bar"] += 1
        else:
            stage["admitted"] += 1
        if m["jaccard"] < bar:
            below_bar.append(m)

        lsh = m["banded"] and m["hamming"] <= pipeline.SIMHASH_HAMMING_MAX
        title = m["same_title"] and m["contemporaneous"]
        reachable_by["both" if lsh and title else "lsh_only" if lsh
                     else "title_only" if title else "neither"] += 1

    total = len(gold)
    print(f"split={split}  可计分金标对 {total}  候选文档 {len(candidates)}  "
          f"排除 {len(excluded)}")
    print("\n— 逐级归因（admitted 是通过的，其余是漏掉的）")
    for key in ("admitted", "no_band_collision", "hamming_over_ceiling",
                "below_similarity_bar", "cross_entity"):
        print(f"    {key:<24}{stage[key]:>6}  {stage[key] / total * 100:5.1f}%")
    missed = total - stage["admitted"]
    print(f"    {'（漏掉合计）':<22}{missed:>6}")

    if below_bar:
        print("\n— 卡在相似度门槛的对：Jaccard 说不像，包含度说像")
        for name, key in (("Jaccard", "jaccard"), ("包含度", "containment"),
                          ("长度比", "length_ratio")):
            values = [m[key] for m in below_bar]
            print(f"    {name:<8}中位 {statistics.median(values):.3f}   "
                  f"p90 {sorted(values)[int(len(values) * 0.9)]:.3f}")
        wide = sum(1 for m in below_bar if m["length_ratio"] > 3)
        titled = sum(1 for m in below_bar if m["same_title"])
        print(f"    长度比 >3x 的 {wide}/{len(below_bar)}；同标题的 {titled}/{len(below_bar)}")

    print("\n— 两条候选路各自够到多少（§7bis.6）")
    for key in ("both", "lsh_only", "title_only", "neither"):
        print(f"    {key:<12}{reachable_by[key]:>6}  {reachable_by[key] / total * 100:5.1f}%")
    ceiling = total - reachable_by["neither"]
    print(f"    召回上限 {ceiling}/{total} = {ceiling / total:.4f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=SPLITS, default="train")
    args = parser.parse_args(argv)
    with connect() as db:
        report(db, args.split)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
