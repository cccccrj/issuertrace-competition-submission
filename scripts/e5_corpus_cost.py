#!/usr/bin/env python3
"""What a near-duplicate rule costs on the delivery corpus, where there is no gold.

The gold is 60-odd financial news outlets.  A quarter of the platform's own
corpus is 巨潮 filings, and the two behave differently enough that a threshold
chosen on one can be wrong on the other -- 「H股公告」 is a heading reused across
years for unrelated filings, and no news headline works that way.  Nothing can
adjudicate those pairs, so the honest move is to count and sample them rather
than to let the gold's silence pass for approval.

The method is summarized in docs/submission/04-模型与算法.md. This script reports
existing clusters and, for each candidate bar:

  * how many same-title pairs the current shipped gate already merges;
  * how many the containment route would add on top, by document type;
  * a sample of the added pairs, so the number can be read rather than trusted.

Read-only.  Opens the delivery corpus in read-only mode and writes nothing.
"""
from __future__ import annotations

import argparse
import collections
import itertools
import pathlib
import random
import sqlite3

from processor import pipeline

CORPUS = pathlib.Path("data/state/collector.sqlite3")


def corpus_documents(db: sqlite3.Connection) -> dict:
    """Every included document, shaped the way the clustering code sees it."""
    band_width = 64 // pipeline.SIMHASH_BANDS
    mask = (1 << band_width) - 1
    out = {}
    for row in db.execute("""SELECT d.version_id, d.company_id, d.title, d.published_at,
            d.document_type, p.text_path
        FROM document_versions d JOIN processed_documents p ON p.version_id = d.version_id
        WHERE d.company_id IS NOT NULL AND p.inclusion_status = 'included'
          AND p.text_path IS NOT NULL"""):
        path = pathlib.Path(row["text_path"])
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        if len(text) < 100:
            continue
        tokens = pipeline.tokenize(f"{row['title'] or ''}\n{text[:12000]}")
        if len(tokens) < 8:
            continue
        signature = pipeline.simhash(tokens)
        out[row["version_id"]] = {
            "row": row, "tokens": tokens, "signature": signature, "text": text,
            "bands": frozenset((band, (signature >> (band * band_width)) & mask)
                               for band in range(pipeline.SIMHASH_BANDS)),
            "title": pipeline.normalize_title(row["title"]),
        }
    return out


def existing_clusters(db: sqlite3.Connection, limit: int) -> None:
    print("— 现有近重复簇中最大的几个（§7bis.8）")
    for row in db.execute("""SELECT c.member_count, v.title, v.document_type
        FROM duplicate_clusters c
        JOIN document_versions v ON v.version_id = c.representative_version_id
        ORDER BY c.member_count DESC LIMIT ?""", (limit,)):
        title = (row["title"] or "").strip() or "（无标题）"
        print(f"    {row['member_count']:>5} 篇  [{row['document_type']}]  {title[:44]}")


def cost(info: dict, bars: list[float], sample: int) -> None:
    buckets = collections.defaultdict(list)
    for version_id, item in info.items():
        if item["title"]:
            buckets[(item["row"]["company_id"], item["title"])].append(version_id)

    already = 0
    added = {bar: collections.Counter() for bar in bars}
    examples = {bar: [] for bar in bars}
    for members in buckets.values():
        for left_id, right_id in itertools.combinations(members, 2):
            left, right = info[left_id], info[right_id]
            overlap = len(left["tokens"] & right["tokens"])
            union = len(left["tokens"] | right["tokens"])
            smaller = min(len(left["tokens"]), len(right["tokens"]))
            jaccard = overlap / union if union else 0.0
            containment = overlap / smaller if smaller else 0.0
            shipped = (bool(left["bands"] & right["bands"])
                       and pipeline.hamming(left["signature"], right["signature"])
                       <= pipeline.SIMHASH_HAMMING_MAX
                       and jaccard >= pipeline.TITLE_MATCH_THRESHOLD)
            if shipped:
                already += 1
                continue
            if not pipeline.within_days(left["row"]["published_at"],
                                        right["row"]["published_at"],
                                        pipeline.REPRINT_GAP_DAYS):
                continue
            for bar in bars:
                if containment >= bar:
                    added[bar][left["row"]["document_type"]] += 1
                    examples[bar].append((containment, left_id, right_id))

    print(f"\n— 同标题对中，现行上线闸门已经合并的：{already}")
    print(f"\n{'包含度门槛':>10}{'新增对':>8}   按文档类型")
    for bar in bars:
        total = sum(added[bar].values())
        breakdown = "  ".join(f"{kind} {count}" for kind, count in added[bar].most_common(4))
        marker = " ←上线" if bar == pipeline.TITLE_CONTAINMENT_THRESHOLD else ""
        print(f"{bar:>10.2f}{total:>8}   {breakdown}{marker}")

    shipped_bar = pipeline.TITLE_CONTAINMENT_THRESHOLD
    chosen = examples.get(shipped_bar) or []
    if chosen and sample:
        print(f"\n— 上线门槛 {shipped_bar} 新增的对，抽样 {min(sample, len(chosen))}：")
        random.seed(0)
        for containment, left_id, right_id in random.sample(chosen, min(sample, len(chosen))):
            left, right = info[left_id], info[right_id]
            title = (left["row"]["title"] or "").strip() or "（无标题）"
            print(f"    包含度 {containment:.3f}  [{left['row']['document_type']}]  {title[:40]}")
            for side in (left, right):
                head = side["text"][:76].replace("\n", " ")
                print(f"        {side['row']['published_at'] or '无日期':<12} {head}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", default="0.90,0.80,0.70,0.50")
    parser.add_argument("--sample", type=int, default=4)
    parser.add_argument("--corpus", type=pathlib.Path, default=CORPUS)
    args = parser.parse_args(argv)
    db = sqlite3.connect(f"file:{args.corpus}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    existing_clusters(db, 8)
    cost(corpus_documents(db), [float(bar) for bar in args.bars.split(",")], args.sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
