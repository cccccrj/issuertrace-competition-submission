"""Evaluation results — 作品提交第 6 项「测试结果与案例展示」.

`data/state/benchmark.sqlite3` holds 26 runs against the organiser's own
labelled dataset, which this system had never seen during development and whose
train/test split the organiser drew. None of it was visible in the interface.

Two disciplines the routes carry rather than leave to the page:

* **Headline figures come from `test`.** 交付物11 fixed that rule for itself and
  it holds here: a train number quoted without its split is the most flattering
  arrangement of the same work. The summary reads test and says so; train is
  available beside it, labelled.
* **A missing database is a state, not an error.** The deploy package ships the
  corpus and not the benchmark, so an installation without it must show
  「评测数据未随本次部署分发」 rather than a 500.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query

from api import benchmark_db
from api.errors import reference

router = APIRouter(prefix="/api/benchmark", tags=["benchmark"])

TASK_LABELS = {
    "e1-issue_codes": "E1 专题分类（规则）",
    "e1-event_types": "E1 事件类型分类（规则）",
    "e1-model": "E1 专题分类（系统研判）",
    "e1-event-types-model": "E1 事件类型分类（系统研判）",
    "e3-rule": "E3 影响方向（规则）",
    "e3-model": "E3 影响方向（系统研判）",
    "e5": "E5 近重复聚合与去重",
    "e5-sweep": "E5 门限扫描",
}
#: The metric each task is judged on, in the order it should be read.
HEADLINE_METRICS = {
    "e5": ("b3_f1", "pair_f1", "pair_precision", "pair_recall"),
    "e1-issue_codes": ("micro_f1", "macro_f1"),
    "e1-event_types": ("micro_f1", "macro_f1"),
    "e1-model": ("model_micro_f1", "model_macro_f1", "rule_micro_f1", "rule_macro_f1"),
    # 同分母的一对：模型与规则在同一批抽样行上各跑一次。
    "e1-event-types-model": ("model_micro_f1", "model_macro_f1", "rule_micro_f1", "rule_macro_f1"),
    "e3-model": ("accuracy", "macro_f1"),
}
#: Some runs keep their headline inside a detail payload rather than as a
#: scalar — E3 evaluates two fields and stores a block per field. The pair
#: names the metric row and the key to lift out of it.
HEADLINE_DETAILS = {
    "e3-rule": (("impact_direction", "accuracy"), ("impact_direction", "macro_f1")),
}
UNAVAILABLE = {"available": False, "reason": "评测数据未随本次部署分发"}

# 对外的评测记录只讲任务、数据划分、时间与指标（I-06）：运行编号（换成短编号）、
# 模型名、提示词版本、代码版本、数据集与映射版本、运行参数都留在评测库里。
HIDDEN_RUN_FIELDS = {"run_id", "model_name", "prompt_version", "code_rev", "params_json",
                     "dataset_version", "map_version", "notes"}


HIDDEN_DETAIL_KEYS = {"model", "provider", "prompt", "prompt_versions", "gateway", "tokens", "usage"}


def public_metric(name: str) -> str:
    """``model_micro_f1`` → ``system_micro_f1``：指标名里也不说实现方式。"""
    return name.replace("model_", "system_").replace("_model", "_system")


def scrub(value: Any) -> Any:
    """细节载荷递归换键名，并丢掉出处字段。"""
    if isinstance(value, dict):
        return {public_metric(str(key)): scrub(item) for key, item in value.items()
                if key not in HIDDEN_RUN_FIELDS and key not in HIDDEN_DETAIL_KEYS}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def public_task(task: str) -> str:
    return task.replace("-model", "-system")


def internal_task(task: str | None) -> str | None:
    if not task:
        return task
    for name in TASK_LABELS:
        if public_task(name) == task:
            return name
    return task


def public_run(row: Any) -> dict[str, Any]:
    run = {key: row[key] for key in row.keys() if key not in HIDDEN_RUN_FIELDS}
    run["task"] = public_task(row["task"])
    run["run_ref"] = reference(row["run_id"], "R")
    run["label"] = TASK_LABELS.get(row["task"], row["task"])
    return run


def resolve_run(db, run_ref: str) -> Any:
    """短编号换回运行记录：评测库只有几十次运行，逐个算一遍即可。"""
    for row in db.execute("SELECT * FROM benchmark_runs"):
        if reference(row["run_id"], "R") == run_ref:
            return row
    return None


@router.get("/summary")
def summary() -> dict[str, Any]:
    """The headline for each task, from the test split where one exists."""
    if not benchmark_db.available():
        return {**UNAVAILABLE, "tasks": []}
    db = benchmark_db.connect()
    try:
        tasks = []
        for task in db.execute("""SELECT DISTINCT task FROM benchmark_runs
                                  WHERE status='ok' ORDER BY task"""):
            name = task["task"]
            # Prefer test; fall back to train only when a task has no test run,
            # and say which was used rather than letting the reader assume.
            row = db.execute("""SELECT * FROM benchmark_runs
                WHERE task=? AND status='ok'
                ORDER BY CASE split WHEN 'test' THEN 0 ELSE 1 END, completed_at DESC
                LIMIT 1""", (name,)).fetchone()
            if not row:
                continue
            wanted = HEADLINE_METRICS.get(name, ())
            metrics = {metric["metric"]: metric["value"] for metric in db.execute(
                "SELECT metric, value FROM benchmark_metrics WHERE run_id=? AND value IS NOT NULL",
                (row["run_id"],))}
            for metric_name, key in HEADLINE_DETAILS.get(name, ()):
                detail = db.execute(
                    "SELECT detail_json FROM benchmark_metrics WHERE run_id=? AND metric=?",
                    (row["run_id"], metric_name)).fetchone()
                if not detail or not detail["detail_json"]:
                    continue
                try:
                    payload = json.loads(detail["detail_json"])
                except ValueError:
                    continue
                if key in payload:
                    metrics.setdefault(key, payload[key])
                    wanted = wanted or (key,)
            if not wanted:
                wanted = tuple(sorted(metrics))
            tasks.append({
                "task": public_task(name),
                "label": TASK_LABELS.get(name, name),
                "split": row["split"],
                "run_ref": reference(row["run_id"], "R"),
                "completed_at": row["completed_at"],
                "headline": [{"metric": public_metric(metric), "value": metrics[metric]}
                             for metric in wanted if metric in metrics][:4],
            })
        return {"available": True, "tasks": tasks}
    finally:
        db.close()


@router.get("/runs")
def runs(task: str | None = None, split: str | None = None,
         limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    if not benchmark_db.available():
        return {**UNAVAILABLE, "items": []}
    db = benchmark_db.connect()
    try:
        filters, parameters = [], []
        for column, value in (("task", internal_task(task)), ("split", split)):
            if value:
                filters.append(f"{column}=?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = db.execute(f"""SELECT * FROM benchmark_runs {where}
            ORDER BY completed_at DESC, started_at DESC LIMIT ?""",
            [*parameters, limit]).fetchall()
        return {"available": True, "items": [public_run(row) for row in rows]}
    finally:
        db.close()


@router.get("/runs/{run_ref}")
def run(run_ref: str) -> dict[str, Any]:
    """One run with every metric it recorded, scalars and detail alike."""
    if not benchmark_db.available():
        return {**UNAVAILABLE, "run": None}
    db = benchmark_db.connect()
    try:
        row = resolve_run(db, run_ref)
        if not row:
            return {"available": True, "run": None, "scalars": [], "details": {}}
        scalars, details = [], {}
        for metric in db.execute("""SELECT metric, value, support, detail_json
                FROM benchmark_metrics WHERE run_id=? ORDER BY metric""", (row["run_id"],)):
            # 有几次运行把出处本身也记成了一条「指标」（metric 名就叫 model）。
            if metric["metric"] in HIDDEN_DETAIL_KEYS or metric["metric"] in HIDDEN_RUN_FIELDS:
                continue
            if metric["value"] is not None:
                scalars.append({"metric": public_metric(metric["metric"]), "value": metric["value"],
                                "support": metric["support"]})
            elif metric["detail_json"]:
                try:
                    details[public_metric(metric["metric"])] = scrub(json.loads(metric["detail_json"]))
                except ValueError:
                    pass
        return {"available": True,
                "run": public_run(row),
                "scalars": scalars, "details": details}
    finally:
        db.close()


@router.get("/dataset")
def dataset() -> dict[str, Any]:
    """What the evaluation was run against, so a figure has a denominator."""
    if not benchmark_db.available():
        return {**UNAVAILABLE, "splits": []}
    db = benchmark_db.connect()
    try:
        def rows(query: str) -> list[dict[str, Any]]:
            try:
                return [dict(row) for row in db.execute(query)]
            except Exception:
                return []
        return {
            "available": True,
            "splits": rows("""SELECT split AS value, count(*) AS count
                              FROM gold_articles GROUP BY 1 ORDER BY value"""),
            "sheets": rows("""SELECT sheet AS value, count(*) AS count
                              FROM gold_articles GROUP BY 1 ORDER BY count DESC"""),
        }
    finally:
        db.close()
