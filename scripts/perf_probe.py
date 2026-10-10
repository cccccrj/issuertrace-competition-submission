#!/usr/bin/env python3
"""Measure what the platform's pages actually cost (W9: page queries P95 ≤ 2s).

A target stated in a plan and never measured is a hope.  This drives the same
endpoints the pages call, against the real corpus, and reports the distribution
rather than an average — an average hides the one query that makes a demo look
broken, which is the only thing P95 was chosen to catch.

Deliberately not a unit test: it needs a running platform and the full database,
and a test that silently passes on an empty fixture would be worse than no test.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Any

import requests

# One entry per page the platform serves, with the query the page issues on
# open.  A page absent from this list is a page nobody has measured.
PROBES = [
    ("总览", "/api/overview", {}),
    # 全局检索一次要问七张表，其中披露原文那一路走 FTS 且不传 company（跨公司
    # 循环），是这条链路最慢的分支——量的就是它。
    ("全局检索", "/api/search", {"q": "募集资金", "limit": 5}),
    ("风险预警", "/api/alerts", {"limit": 25}),
    ("预警筛选面板", "/api/alerts/facets", {}),
    # 风险事项库与研判档案是重组后的主动线。这张表的规矩是「一个不在表里的页面，
    # 就是没人量过的页面」——新的核心页必须在表里。
    ("风险事项库", "/api/risks", {"limit": 25}),
    ("风险预警中心", "/api/risks/summary", {}),
    ("研判档案", "/api/risks/{alert_id}", {}),
    ("事件台账", "/api/events", {"limit": 25}),
    # 事件详情——「答辩演示的核心页面」，此前不在这张表里，而 2026-09-06 又给它加了
    # 四个 LEFT JOIN（四张 labels 表的判据）。`{event_id}` 由 run() 先取一条真实 id 代入。
    ("事件详情", "/api/events/{event_id}", {}),
    ("时间线", "/api/timeline", {"limit": 50}),
    ("阶段", "/api/timeline/phases", {}),
    ("主张核验", "/api/claims", {"limit": 50}),
    ("主张统计", "/api/claims/stats", {}),
    ("议题台账", "/api/issues", {"limit": 25}),
    ("传播链路", "/api/propagation", {"limit": 25}),
    ("披露差异", "/api/diffs", {"limit": 25}),
    ("复核队列", "/api/review/tasks", {"limit": 20}),
    ("复核指标", "/api/review/metrics", {}),
    ("通知箱", "/api/notifications", {"limit": 25}),
    ("审计·处理运行", "/api/audit/runs", {"limit": 50}),
    ("审计·版本足迹", "/api/audit/versions", {}),
    # 词表页：每个词条现在带定义与判定规则，长度比原来大一个量级。
    ("主数据词表", "/api/vocabularies", {}),
]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def run(base: str, username: str, password: str, rounds: int, budget_ms: float) -> dict[str, Any]:
    session = requests.Session()
    session.trust_env = False  # a local probe must not go through a proxy
    login = session.post(f"{base}/api/auth/login",
                         json={"username": username, "password": password}, timeout=30)
    login.raise_for_status()
    # 详情页需要一个真实事件 id；取一条即可，选哪一条不影响这条链路的形状。
    sample = session.get(f"{base}/api/events", params={"limit": 1}, timeout=60).json()
    event_id = (sample.get("items") or [{}])[0].get("event_id", "")
    # 同理，研判档案要一条真实事项 id。写死的 id 会在下一次重算预警时失效，
    # 那时脚本会安静地测一个 404 的耗时。
    risks = session.get(f"{base}/api/risks", params={"limit": 1}, timeout=60).json()
    alert_id = (risks.get("items") or [{}])[0].get("alert_id", "")
    results = []
    for name, path, params in PROBES:
        path = path.replace("{event_id}", event_id).replace("{alert_id}", alert_id)
        samples, failures = [], 0
        for _ in range(rounds):
            started = time.perf_counter()
            response = session.get(f"{base}{path}", params=params, timeout=60)
            samples.append((time.perf_counter() - started) * 1000)
            failures += 0 if response.ok else 1
        results.append({"page": name, "path": path, "failures": failures,
                        "p50_ms": round(statistics.median(samples), 1),
                        "p95_ms": round(percentile(samples, 0.95), 1),
                        "max_ms": round(max(samples), 1),
                        "within_budget": percentile(samples, 0.95) <= budget_ms})
    worst = max(results, key=lambda row: row["p95_ms"])
    return {"base": base, "rounds": rounds, "budget_ms": budget_ms,
            "slowest_page": worst["page"], "slowest_p95_ms": worst["p95_ms"],
            "all_within_budget": all(row["within_budget"] for row in results),
            "failures": sum(row["failures"] for row in results), "probes": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure page query latency against a running platform")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="reviewer")
    parser.add_argument("--password", required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--budget-ms", type=float, default=2000)
    args = parser.parse_args()
    print(json.dumps(run(args.base, args.username, args.password, args.rounds, args.budget_ms),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
