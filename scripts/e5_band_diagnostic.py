#!/usr/bin/env python3
"""Why near-duplicate recall stops where it does, without running the clustering.

Recall on E5 is capped upstream of the similarity gates: two documents are only
compared if their SimHash signatures collide in at least one band, so any gold
pair that never collides is lost before a threshold is consulted.  That ceiling
is a property of the gold and the signatures alone, which means it can be
computed directly -- no clustering run, no parameter sweep, seconds instead of
the minute a single grid point costs.

The clustering method is summarized in docs/submission/04-模型与算法.md;
this script reproduces the detailed band-collision diagnostic.
It answers two questions the sweep cannot:

  * how high could recall go at a given band count (and is the shipped value
    leaving recall on the table);
  * when train and test disagree, is that overfitting or is the test split
    simply carrying duplicates that sit further apart.

Read-only.  Touches the benchmark database only, never the delivery corpus.
"""
from __future__ import annotations

import argparse
import collections

from processor import pipeline
from processor.benchmark import (SPLITS, cluster_tokens, connect, gold_clusters,
                                 gold_documents, _pairs)

BAND_COUNTS = (4, 8, 16, 32)
HAMMING_BUCKETS = ((8, "<=8"), (16, "9-16"), (32, "17-32"))


def entity_lookup(db, split: str):
    """Each article's entity, inheriting a parent's when the child carries none.

    Clustering groups by entity before it compares anything, so a gold pair whose
    ends sit under different entities can never be recovered.  The inheritance
    mirrors what the harness does when it scores, and the cross-entity count is
    reported rather than assumed to be zero.
    """
    direct = {r["article_file_id"]: r["entity_id"] for r in db.execute(
        "SELECT article_file_id, entity_id FROM gold_articles WHERE split=?", (split,))}
    inherited: dict[str, str] = {}
    for row in db.execute(
            """SELECT l.child_article_id AS child, p.entity_id AS entity
                 FROM gold_duplicate_links l
                 JOIN gold_articles c ON c.article_file_id = l.child_article_id
                 JOIN gold_articles p ON p.article_file_id = l.parent_article_id
                WHERE c.split = ? AND p.split = ? AND p.entity_id IS NOT NULL""",
            (split, split)):
        inherited.setdefault(row["child"], row["entity"])
    return lambda article: direct.get(article) or inherited.get(article)


def bands_collide(left: int, right: int, bands: int) -> bool:
    width = 64 // bands
    mask = (1 << width) - 1
    return any(((left >> (b * width)) & mask) == ((right >> (b * width)) & mask)
               for b in range(bands))


def diagnose(db, split: str, hamming_max: int) -> None:
    candidates, version_to_article, _ = gold_documents(db, split)
    universe = set(version_to_article.values())
    gold_pairs = _pairs(gold_clusters(db, split), universe)
    entity_of = entity_lookup(db, split)
    tokens = cluster_tokens(candidates)
    signature = {doc.version_id: pipeline.simhash(tokens[doc.version_id])
                 for doc, _ in candidates}
    article_to_version = {a: v for v, a in version_to_article.items()}

    same, cross = [], 0
    for pair in gold_pairs:
        left, right = list(pair)
        if entity_of(left) == entity_of(right):
            same.append((left, right))
        else:
            cross += 1

    distances = {}
    for left, right in same:
        distances[(left, right)] = pipeline.hamming(
            signature[article_to_version[left]], signature[article_to_version[right]])
    spread = collections.Counter()
    for distance in distances.values():
        for ceiling, label in HAMMING_BUCKETS:
            if distance <= ceiling:
                spread[label] += 1
                break
        else:
            spread[">32"] += 1

    total = len(gold_pairs)
    print(f"\n=== {split} ===")
    print(f"可计分金标对 {total}；跨主体 {cross}（本系统按主体分组，永远抓不到）")
    print("  汉明分布: " + "  ".join(
        f"{label} {spread[label]} ({spread[label]/total:.1%})"
        for _, label in HAMMING_BUCKETS + ((0, ">32"),) if label in spread))
    print(f"  {'bands':>6}  {'可比较':>8}  {'占金标':>7}  {f'且汉明<={hamming_max}':>12}  {'召回上限':>8}")
    for bands in BAND_COUNTS:
        comparable = gated = 0
        for pair in same:
            left, right = pair
            if bands_collide(signature[article_to_version[left]],
                             signature[article_to_version[right]], bands):
                comparable += 1
                if distances[pair] <= hamming_max:
                    gated += 1
        print(f"  {bands:6d}  {comparable:8d}  {comparable/total:6.1%}  "
              f"{gated:12d}  {gated/total:7.1%}")

    sizes = collections.Counter(
        len(cluster & universe) for cluster in gold_clusters(db, split).values()
        if len(cluster & universe) > 1)
    print("  金标簇规模（可计分）:", dict(sorted(sizes.items())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", action="append", choices=SPLITS,
                        help="repeatable; default: every split")
    parser.add_argument("--hamming-max", type=int, default=pipeline.SIMHASH_HAMMING_MAX,
                        help="the shipped ceiling unless overridden")
    args = parser.parse_args()
    db = connect()
    for split in (args.split or list(SPLITS)):
        diagnose(db, split, args.hamming_max)


if __name__ == "__main__":
    main()
