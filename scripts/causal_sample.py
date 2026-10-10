#!/usr/bin/env python3
"""Draw a stratified sample of causal edges as a worksheet to read by hand.

There is no third-party gold for event-to-event causality — the organiser's
dataset is news with event labels, not a chain — so the only thing that can be
said about this layer is what someone finds when they read its output.  This
writes that reading list.

Three strata, and the reasons differ:

* **relation** — equal shares of the six values, so the four rare ones are not
  crowded out by the refusals.  A sample proportional to the corpus would be
  seven parts CONTEXT_ONLY and would say nothing about REVISES at all.
* **candidate_path** — ``prior_edge`` is deliberately over-sampled.  Those are
  the pairs the old similarity rule already accepted as FOLLOW_UP, and what this
  layer decides about them is the whole question of whether it earns its cost.
* **time_precision_low** — pairs ordered by a coarse event clock are listed
  separately rather than mixed in, so a reader can tell a dating problem from a
  judgement problem.

Read-only.  It writes a Markdown file and touches nothing in the database.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from collections import defaultdict
from typing import Any

from collector.collect import ROOT
from processor.causal_link import CAUSAL_KINDS, PROMPT_VERSION

DB_PATH = ROOT / "data" / "state" / "collector.sqlite3"
# Enough of each side to judge the link without turning the worksheet into the
# corpus; a reader who needs more opens the event page named on the row.
EXCERPT = 220
# Over-sample factor for the control group, see the module docstring.
PRIOR_EDGE_SHARE = 0.30

QUERY = """SELECT c.edge_id, c.company_id, c.relation, c.method, c.candidate_path,
                  c.confidence, c.reason, c.quote_from, c.quote_to, c.gap_hours,
                  c.shared_element_count, c.prior_edge_kind, c.candidate_note,
                  c.time_precision_low, c.from_event_id, c.to_event_id,
                  ef.summary AS from_summary, et.summary AS to_summary,
                  ef.event_time AS from_time, et.event_time AS to_time,
                  vf.title AS from_title, vt.title AS to_title,
                  vf.published_at AS from_published, vt.published_at AS to_published
    FROM event_causal_edges c
    JOIN event_records ef ON ef.event_id=c.from_event_id
    JOIN event_records et ON et.event_id=c.to_event_id
    JOIN document_versions vf ON vf.version_id=ef.version_id
    JOIN document_versions vt ON vt.version_id=et.version_id
    WHERE c.prompt_version=?"""


def deterministic_order(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Shuffle by a hash of the edge id: spread out, and identical between runs."""
    return sorted(rows, key=lambda row: hashlib.sha256(row["edge_id"].encode()).hexdigest())


def stratify(rows: list[sqlite3.Row], size: int) -> list[sqlite3.Row]:
    control = [row for row in rows if row["candidate_path"] == "prior_edge"]
    rest = [row for row in rows if row["candidate_path"] != "prior_edge"]
    wanted_control = min(len(control), round(size * PRIOR_EDGE_SHARE))
    picked = deterministic_order(control)[:wanted_control]

    by_relation: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in deterministic_order(rest):
        by_relation[row["relation"]].append(row)
    # Round-robin across the relations rather than a fixed share each: the four
    # rare values would otherwise leave their quota unfilled and shrink the
    # sample instead of handing the room back.
    order = [relation for relation in CAUSAL_KINDS if by_relation[relation]]
    index = 0
    while len(picked) < size and order:
        relation = order[index % len(order)]
        if by_relation[relation]:
            picked.append(by_relation[relation].pop(0))
        else:
            order.remove(relation)
            continue
        index += 1
    return picked


def excerpt(value: Any) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:EXCERPT] + ("…" if len(text) > EXCERPT else "")


def render(rows: list[sqlite3.Row], total: int) -> str:
    lines = ["# 因果边自查工作表", "",
             f"> 抽样自 `event_causal_edges`（`{PROMPT_VERSION}`，全库 {total} 条），"
             f"共 {len(rows)} 对。分层见 `scripts/causal_sample.py` 的模块说明。", "",
             "> 每条逐项判：**关系类型对不对 / 方向对不对 / 引证是不是原话**；"
             "判错的写清楚错在候选、方向、类型还是引证。", ""]
    for ordinal, row in enumerate(rows, start=1):
        flag = "（事件时钟粗糙）" if row["time_precision_low"] else ""
        prior = f"，旧边 {row['prior_edge_kind']}" if row["prior_edge_kind"] else ""
        shared = (f"，共享要素 {row['shared_element_count']} 个"
                  if row["shared_element_count"] else "")
        lines += [
            f"## {ordinal}. {row['relation']} · {row['company_id']} {flag}",
            f"- 路径：`{row['candidate_path']}`{prior}{shared}；判法：`{row['method']}`；"
            f"自报置信度 {row['confidence'] if row['confidence'] is not None else '—'}；"
            f"相隔 {round((row['gap_hours'] or 0) / 24)} 天",
            f"- 前：**{excerpt(row['from_title'])}**（{row['from_published']}，"
            f"事件时间 {row['from_time'] or '—'}）",
            f"  - {excerpt(row['from_summary'])}",
            f"- 后：**{excerpt(row['to_title'])}**（{row['to_published']}，"
            f"事件时间 {row['to_time'] or '—'}）",
            f"  - {excerpt(row['to_summary'])}",
            f"- 判据：{excerpt(row['reason'])}",
            f"- 引前：{excerpt(row['quote_from']) or '—'}",
            f"- 引后：{excerpt(row['quote_to']) or '—'}",
        ]
        if row["candidate_note"]:
            lines.append(f"- 候选备注：{excerpt(row['candidate_note'])}")
        lines += [f"- 事件页：`/events/{row['from_event_id']}` → `/events/{row['to_event_id']}`",
                  "- **判定**：类型 □对 □错（应为 ____） 方向 □对 □错 引证 □原话 □定位不到",
                  ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=30)
    parser.add_argument("--company")
    parser.add_argument("--out", default="-", help="output path, or - for stdout")
    args = parser.parse_args()
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(QUERY, (PROMPT_VERSION,)).fetchall()
    if args.company:
        rows = [row for row in rows if row["company_id"] == args.company]
    text = render(stratify(rows, args.size), len(rows))
    db.close()
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
