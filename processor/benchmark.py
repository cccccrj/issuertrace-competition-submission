#!/usr/bin/env python3
"""Evaluation harness for the competition's third-party labelled dataset.

The dataset is gold, not corpus.  Everything here therefore lives in its own
SQLite file next to the real one rather than inside it, for four reasons that
are each independently sufficient:

``processor.verify`` requires ``processed_documents`` to cover every
non-rejected ``document_versions`` row, so a single gold row inserted into the
delivered database breaks an invariant that is supposed to hold.  ``api/db.py``
hard-codes the delivered database read-only, so a sibling file can never surface
on a page as if it were collected evidence.  Nothing here writes
``event_records`` or ``event_claims``, so the ``claim_id = sha256(event_id,
ordinal)`` hazard — re-labelling keeps the id and replaces the text under it,
silently re-pointing 2,853 adjudications — is structurally unreachable.  And
``scripts/package_deploy.sh`` copies the delivered database by name, so the
delivery archive stays free of third-party full text.

Provenance is recorded in ``docs/命题二-总体方案-Pro.md`` §3.4.  The dataset is
the organiser's, distributed to every team; it is not self-collected and must
never be counted in the collection ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import zipfile
from collections import Counter
from itertools import product
from pathlib import Path
from typing import Any

from api.metrics import class_scores
from processor import pipeline, propagation
from processor.pipeline import utc_now
from processor.xlsx_reader import excel_serial_to_iso, rows

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BENCHMARK_DB = DATA / "state" / "benchmark.sqlite3"
GOLD_DIR = DATA / "gold"
DATASET_ZIP = DATA / "事件驱动智能识别可信评估与脉络追踪.zip"
DATASET_NAME = "事件驱动智能识别可信评估与脉络追踪"

# Only these members are ours.  The archive also carries two other tracks'
# datasets, which are extracted by nobody and used by nothing.
DATASET_MEMBERS = (
    "news_with_tags_train.xlsx",
    "news_with_tags_test.xlsx",
    "事件类型_标的.json",
    "事件类型_行业.json",
)
POOL_MEMBERS = ("news_without_tags_train.xlsx", "news_without_tags_test.xlsx")

SPLITS = ("train", "test")
COMPANY_SHEET = "个股新闻"
INDUSTRY_SHEET = "行业新闻"
COMPANY_DUPLICATE_SHEET = "个股重复新闻"
INDUSTRY_DUPLICATE_SHEET = "行业重复新闻"
LABELLED_SHEETS = (COMPANY_SHEET, INDUSTRY_SHEET)
DUPLICATE_SHEETS = (COMPANY_DUPLICATE_SHEET, INDUSTRY_DUPLICATE_SHEET)

# Entity keys are prefixed because a company's trading code and an industry's
# code are both bare digits and would otherwise collide in the single
# ``company_id`` slot that the clustering code groups by.
COMPANY_PREFIX = "co:"
INDUSTRY_PREFIX = "ind:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_runs(
    run_id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    split TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    map_version TEXT,
    params_json TEXT NOT NULL,
    code_rev TEXT,
    model_name TEXT,
    prompt_version TEXT,
    sample_cap INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    notes TEXT);

CREATE TABLE IF NOT EXISTS gold_articles(
    article_file_id TEXT PRIMARY KEY,
    split TEXT NOT NULL,
    sheet TEXT NOT NULL,
    entity_kind TEXT,
    entity_id TEXT,
    trading_code TEXT,
    secu_abbr TEXT,
    industry_code TEXT,
    industry_name TEXT,
    article_source TEXT,
    title TEXT,
    published_at TEXT,
    content_hash TEXT NOT NULL,
    text_chars INTEGER NOT NULL,
    version_id TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_gold_split ON gold_articles(split, sheet, entity_id);

-- Bodies live apart so that metric queries, which only ever need labels and
-- ids, do not drag several hundred megabytes of article text through memory.
CREATE TABLE IF NOT EXISTS gold_article_text(
    article_file_id TEXT PRIMARY KEY,
    text TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS gold_event_labels(
    article_file_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    event_emotion TEXT NOT NULL,
    impact_analysis_hash TEXT);

-- The dataset states duplication twice: a main-sheet row lists the ids that
-- duplicate it, and a duplicate-sheet row names the row it duplicates.  Both
-- directions are kept because they do not fully agree -- 345 of 999 train
-- duplicate rows name a parent that is not in the main sheet -- and the
-- disagreement is a fact about the gold, not noise to average away.
CREATE TABLE IF NOT EXISTS gold_duplicate_links(
    child_article_id TEXT NOT NULL,
    parent_article_id TEXT NOT NULL,
    split TEXT NOT NULL,
    link_direction TEXT NOT NULL,
    PRIMARY KEY(child_article_id, parent_article_id, link_direction));
CREATE INDEX IF NOT EXISTS idx_gold_links_parent ON gold_duplicate_links(parent_article_id);

CREATE TABLE IF NOT EXISTS gold_event_schema(
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_name TEXT,
    event_name TEXT NOT NULL,
    description TEXT NOT NULL,
    description_hash TEXT NOT NULL,
    PRIMARY KEY(entity_kind, entity_id, event_name));

CREATE TABLE IF NOT EXISTS benchmark_predictions(
    run_id TEXT NOT NULL,
    article_file_id TEXT NOT NULL,
    task TEXT NOT NULL,
    predicted_label TEXT,
    predicted_json TEXT,
    latency_ms INTEGER,
    PRIMARY KEY(run_id, article_file_id, task));

CREATE TABLE IF NOT EXISTS benchmark_metrics(
    run_id TEXT NOT NULL,
    task TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    support INTEGER,
    detail_json TEXT,
    PRIMARY KEY(run_id, task, metric));

-- Every row the harness declines to score, and why.  Clustering silently drops
-- documents with no entity, under 100 characters or under 8 tokens; a recall
-- figure that hides those in its denominator is not a recall figure.
CREATE TABLE IF NOT EXISTS benchmark_exclusions(
    run_id TEXT NOT NULL,
    article_file_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(run_id, article_file_id));
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else BENCHMARK_DB
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def gold_version_id(article_file_id: str) -> str:
    """A version id that can never collide with a collected document's.

    Real ids are ``sha256(source_id\\0canonical_url\\0content_hash)``; prefixing
    the gold input with a literal keeps the two id spaces disjoint even if a
    gold article and a collected one were somehow identical.
    """
    return hashlib.sha256(f"gold\0{article_file_id}".encode()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract(zip_path: Path = DATASET_ZIP, target: Path = GOLD_DIR,
            *, include_pool: bool = False) -> list[Path]:
    """Unpack just this track's members out of the multi-track archive.

    Member names are matched rather than trusted: the archive stores its local
    filename headers in GBK while the central directory is UTF-8, so unzip(1)
    warns on every entry, and blanket extraction would also unpack two unrelated
    tracks totalling 3.4 GB.
    """
    wanted = set(DATASET_MEMBERS)
    if include_pool:
        wanted |= set(POOL_MEMBERS)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(zip_path) as archive:
        members = {Path(name).name: name for name in archive.namelist()
                   if name.startswith(f"{DATASET_NAME}/") and Path(name).name in wanted}
        missing = wanted - set(members)
        if missing:
            raise FileNotFoundError(f"{zip_path.name} does not contain {sorted(missing)}")
        for base, name in sorted(members.items()):
            destination = target / base
            if destination.exists() and destination.stat().st_size == archive.getinfo(name).file_size:
                written.append(destination)
                continue
            with archive.open(name) as source, destination.open("wb") as sink:
                while chunk := source.read(1 << 20):
                    sink.write(chunk)
            written.append(destination)
    return written


def dataset_version(source: Path = GOLD_DIR) -> str:
    """A short digest over the taxonomy files, identifying which gold this is."""
    digest = hashlib.sha256()
    for name in DATASET_MEMBERS:
        path = source / name
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing; run `benchmark extract` first")
        digest.update(name.encode("utf-8"))
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
    return digest.hexdigest()[:16]


def file_checksums(source: Path = GOLD_DIR) -> dict[str, str]:
    out = {}
    for name in DATASET_MEMBERS:
        path = source / name
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        out[name] = digest.hexdigest()
    return out


def entity_of(sheet: str, row: dict[str, str]) -> tuple[str | None, str | None]:
    """(entity_kind, entity_id) for a labelled row."""
    if sheet == COMPANY_SHEET:
        code = (row.get("trading_code") or "").strip()
        return ("company", COMPANY_PREFIX + code) if code else (None, None)
    if sheet == INDUSTRY_SHEET:
        code = (row.get("industry_code") or "").strip()
        return ("industry", INDUSTRY_PREFIX + code) if code else (None, None)
    return (None, None)


def duplicate_ids(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def import_split(db: sqlite3.Connection, split: str, source: Path = GOLD_DIR) -> dict[str, int]:
    """Load one split's four sheets plus its duplication links."""
    path = source / f"news_with_tags_{split}.xlsx"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; run `benchmark extract` first")
    now = utc_now()
    counts: dict[str, int] = {}
    for sheet in LABELLED_SHEETS + DUPLICATE_SHEETS:
        seen = 0
        for row in rows(path, sheet):
            article_id = (row.get("article_file_id") or "").strip()
            if not article_id:
                continue
            text = row.get("content") or ""
            kind, entity = entity_of(sheet, row)
            db.execute("""INSERT OR REPLACE INTO gold_articles
                (article_file_id,split,sheet,entity_kind,entity_id,trading_code,secu_abbr,
                 industry_code,industry_name,article_source,title,published_at,content_hash,
                 text_chars,version_id,imported_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (article_id, split, sheet, kind, entity,
                 row.get("trading_code") or None, row.get("secu_abbr") or None,
                 row.get("industry_code") or None, row.get("industry_name") or None,
                 row.get("article_source") or None, row.get("article_title") or None,
                 excel_serial_to_iso(row.get("article_publish_time")),
                 sha256_text(text), len(text), gold_version_id(article_id), now))
            db.execute("INSERT OR REPLACE INTO gold_article_text(article_file_id,text) VALUES(?,?)",
                       (article_id, text))
            if sheet in LABELLED_SHEETS:
                db.execute("""INSERT OR REPLACE INTO gold_event_labels
                    (article_file_id,event_name,event_emotion,impact_analysis_hash)
                    VALUES(?,?,?,?)""",
                    (article_id, row.get("event_name") or "", row.get("event_emotion") or "",
                     sha256_text(row.get("event_impact_analysis") or "")))
                for child in duplicate_ids(row.get("duplication_id")):
                    db.execute("""INSERT OR IGNORE INTO gold_duplicate_links
                        (child_article_id,parent_article_id,split,link_direction)
                        VALUES(?,?,?,?)""", (child, article_id, split, "parent_lists_child"))
            else:
                parent = (row.get("duplication_id") or "").strip()
                if parent:
                    db.execute("""INSERT OR IGNORE INTO gold_duplicate_links
                        (child_article_id,parent_article_id,split,link_direction)
                        VALUES(?,?,?,?)""", (article_id, parent, split, "child_names_parent"))
            seen += 1
        counts[sheet] = seen
    db.commit()
    return counts


def import_taxonomy(db: sqlite3.Connection, source: Path = GOLD_DIR) -> dict[str, int]:
    """Load the per-entity event vocabularies.

    Every labelled row draws its ``event_name`` from the schema of its own
    entity, so the taxonomy is the candidate set for the official task, not a
    flat class list.
    """
    counts = {}
    for name, kind, key, id_field, name_field in (
        ("事件类型_标的.json", "company", "company", "trading_code", "secu_abbr"),
        ("事件类型_行业.json", "industry", "industry", "inducode", "induname"),
    ):
        payload = json.loads((source / name).read_text(encoding="utf-8"))
        seen = 0
        for entry in payload:
            entity = entry[key]
            prefix = COMPANY_PREFIX if kind == "company" else INDUSTRY_PREFIX
            entity_id = prefix + str(entity[id_field]).strip()
            for definition in entry.get("event_schema", []):
                description = definition.get("description") or ""
                db.execute("""INSERT OR REPLACE INTO gold_event_schema
                    (entity_kind,entity_id,entity_name,event_name,description,description_hash)
                    VALUES(?,?,?,?,?,?)""",
                    (kind, entity_id, entity.get(name_field), definition["event_name"],
                     description, sha256_text(description)))
                seen += 1
        counts[kind] = seen
    db.commit()
    return counts


def integrity(db: sqlite3.Connection) -> dict[str, Any]:
    """Facts a report has to state before any metric is believable."""
    def one(sql: str, *args) -> int:
        return db.execute(sql, args).fetchone()[0]

    labelled = one("SELECT COUNT(*) FROM gold_event_labels")
    off_schema = one("""SELECT COUNT(*) FROM gold_event_labels l
        JOIN gold_articles a ON a.article_file_id=l.article_file_id
        LEFT JOIN gold_event_schema s
          ON s.entity_id=a.entity_id AND s.event_name=l.event_name
        WHERE a.entity_id IS NOT NULL AND s.event_name IS NULL""")
    overlap = one("""SELECT COUNT(*) FROM gold_articles a
        JOIN gold_articles b ON a.article_file_id=b.article_file_id AND a.split<>b.split""")
    dangling = one("""SELECT COUNT(*) FROM gold_duplicate_links l
        LEFT JOIN gold_articles a ON a.article_file_id=l.parent_article_id
        WHERE a.article_file_id IS NULL""")
    multi_parent = one("""SELECT COUNT(*) FROM (
        SELECT child_article_id FROM gold_duplicate_links
        GROUP BY child_article_id HAVING COUNT(DISTINCT parent_article_id) > 1)""")
    return {
        "labelled_rows": labelled,
        "labels_outside_entity_schema": off_schema,
        "articles_in_both_splits": overlap,
        "links_with_unknown_parent": dangling,
        "children_naming_more_than_one_parent": multi_parent,
    }


def summarise(db: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {"splits": {}}
    for split in SPLITS:
        sheets = {row["sheet"]: row["n"] for row in db.execute(
            "SELECT sheet, COUNT(*) AS n FROM gold_articles WHERE split=? GROUP BY sheet", (split,))}
        emotions = {row["event_emotion"]: row["n"] for row in db.execute(
            """SELECT event_emotion, COUNT(*) AS n FROM gold_event_labels l
               JOIN gold_articles a ON a.article_file_id=l.article_file_id
               WHERE a.split=? GROUP BY event_emotion ORDER BY n DESC""", (split,))}
        links = db.execute("SELECT COUNT(*) FROM gold_duplicate_links WHERE split=?",
                           (split,)).fetchone()[0]
        out["splits"][split] = {"sheets": sheets, "event_emotion": emotions,
                                "duplicate_links": links}
    out["event_schema"] = {row["entity_kind"]: row["n"] for row in db.execute(
        "SELECT entity_kind, COUNT(*) AS n FROM gold_event_schema GROUP BY entity_kind")}
    out["entities"] = {row["entity_kind"]: row["n"] for row in db.execute(
        "SELECT entity_kind, COUNT(DISTINCT entity_id) AS n FROM gold_event_schema GROUP BY entity_kind")}
    out["integrity"] = integrity(db)
    return out



# The grid deliberately brackets the shipped values on both sides rather than
# searching upward from them: a sweep that can only tighten a threshold cannot
# discover that the threshold was already too tight.
DEFAULT_E5_GRID = {
    "title_threshold": [0.50, 0.60, 0.66, 0.72, 0.80, 0.86],
    "body_threshold": [0.72, 0.80, 0.86, 0.90, 0.95],
    "hamming_max": [4, 8, 12, 16],
    "containment_threshold": [0.50, 0.70, 0.80, 0.90],
    "max_reprint_gap_days": [1, 3, 7, 30],
    "template_text_bar": [0.85, 0.90, 0.95],
    "template_number_bar": [0.3, 0.5, 0.7],
    "template_number_share": [0.15, 0.5, 1.0],
}



def fbeta(precision: float, recall: float, beta: float) -> float:
    """F-measure with recall weighted ``beta`` times as heavily as precision.

    F1 is the wrong objective for this particular gate, and the reason is in the
    system's own design.  ``independent_source_count`` is the number of weakly
    connected components left after reprints are removed (总体方案 §4.3), so a
    *missed* duplicate leaves two copies of one story standing as two independent
    sources and **inflates** corroboration -- exactly the failure 关键测试场景 12
    and 17 exist to prevent.  A *false* merge collapses two genuinely independent
    reports into one and deflates it instead.  Both are errors, but only one of
    them makes the platform overstate how well a claim is corroborated, so recall
    is worth more than precision here and F2 is reported beside F1.
    """
    if not precision or not recall:
        return 0.0
    return round((1 + beta ** 2) * precision * recall / (beta ** 2 * precision + recall), 4)


def winning_params(db: sqlite3.Connection, sweep_run_id: str) -> dict[str, Any]:
    """The best grid point of a finished sweep, for a one-shot test run."""
    row = db.execute("""SELECT detail_json FROM benchmark_metrics
        WHERE run_id=? AND metric='sweep'""", (sweep_run_id,)).fetchone()
    if row is None:
        raise KeyError(f"{sweep_run_id} is not a sweep run")
    best = json.loads(row["detail_json"])["best_by_pair_f1"]
    if not best:
        raise ValueError(f"{sweep_run_id} recorded no results")
    return dict(best["params"])


def load_run(db: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = db.execute("SELECT * FROM benchmark_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(f"no such run: {run_id}")
    metrics: dict[str, Any] = {}
    for row in db.execute("SELECT metric, value, support, detail_json FROM benchmark_metrics "
                          "WHERE run_id=?", (run_id,)):
        metrics[row["metric"]] = (json.loads(row["detail_json"])
                                  if row["detail_json"] is not None else row["value"])
    return {"run": dict(run), "metrics": metrics}


def latest_run(db: sqlite3.Connection, task: str) -> str:
    row = db.execute("""SELECT run_id FROM benchmark_runs WHERE task=? AND status='ok'
        ORDER BY started_at DESC LIMIT 1""", (task,)).fetchone()
    if row is None:
        raise KeyError(f"no completed run for task {task!r}")
    return row["run_id"]


def render_report(db: sqlite3.Connection, *, run_id: str | None = None,
                  task: str = "e5", fmt: str = "json") -> str:
    payload = load_run(db, run_id or latest_run(db, task))
    if fmt == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    run, metrics = payload["run"], payload["metrics"]
    lines = [f"### {run['task']} · {run['split']} 切分", "",
             f"- run_id `{run['run_id']}`，代码版本 `{run['code_rev']}`",
             f"- 数据集版本 `{run['dataset_version']}`，参数 `{run['params_json']}`",
             f"- 运行于 {run['started_at']}", ""]
    if "sweep" in metrics:
        sweep = metrics["sweep"]
        scored = [{**item, "pair_f2": fbeta(item["pair_precision"], item["pair_recall"], 2)}
                  for item in sweep["results"]]
        # The columns are the gates this sweep actually varied.  Hard-coding the
        # first four silently rendered a row of empty cells the day a fifth gate
        # was swept, which is how a calibration record stops being a record.
        gates = sorted({key for item in scored for key in item["params"]})
        lines += [f"网格点 {sweep.get('completed', len(scored))}/{sweep.get('of', len(scored))}", "",
                  "| " + " | ".join(gates) + " | pair-P | pair-R | pair-F1 | **pair-F2** | B³-F1 |",
                  "|---:" * (len(gates) + 5) + "|"]
        for item in sorted(scored, key=lambda x: -x["pair_f2"])[:20]:
            p = item["params"]
            lines.append(
                "| " + " | ".join(str(p.get(gate, "")) for gate in gates)
                + f" | {item['pair_precision']} | {item['pair_recall']} "
                f"| {item['pair_f1']} | **{item['pair_f2']}** | {item['b3_f1']} |")
        best_f1 = max(scored, key=lambda x: x["pair_f1"])
        best_f2 = max(scored, key=lambda x: x["pair_f2"])
        lines += ["", f"- 当前上线值：`{sweep['shipped']}`",
                  f"- train argmax F1：`{best_f1['params']}`"
                  f"（P {best_f1['pair_precision']} / R {best_f1['pair_recall']} "
                  f"/ F1 {best_f1['pair_f1']} / F2 {best_f1['pair_f2']}）",
                  f"- train argmax **F2**：`{best_f2['params']}`"
                  f"（P {best_f2['pair_precision']} / R {best_f2['pair_recall']} "
                  f"/ F1 {best_f2['pair_f1']} / F2 {best_f2['pair_f2']}）", ""]
        return "\n".join(lines)
    if "per_class" in metrics and "micro_f1" in metrics:
        lines += [f"- 映射版本 `{metrics.get('map_version')}`，词表 `{metrics.get('layer')}`", "",
                  "| 指标 | 值 |", "|---|---:|"]
        for key in ("micro_precision", "micro_recall", "micro_f1", "macro_f1",
                    "exact_set_match", "any_overlap"):
            if key in metrics:
                lines.append(f"| {key} | {metrics[key]} |")
        baselines = metrics.get("baselines") or {}
        if baselines.get("most_frequent_code"):
            best = baselines["most_frequent_code"]
            lines += ["", "| 基线 | micro-F1 | macro-F1 | any-overlap |", "|---|---:|---:|---:|",
                      f"| 恒答最频类「{best.get('label')}」 | {best['micro_f1']} "
                      f"| {best['macro_f1']} | {best['any_overlap']} |"]
        if baselines.get("schema_candidate"):
            sc = baselines["schema_candidate"]
            lines += ["", f"官方赛道任务（本系统不执行）的平凡策略「{sc['strategy']}」"
                          f"准确率 {sc['accuracy']}，其中 {sc['single_candidate_share']} "
                          f"的行该主体只有唯一候选（样本 {sc['sample_size']}）。"]
        lines += ["", "| 类别 | P | R | F1 | 金标支持 | 预测次数 |", "|---|---:|---:|---:|---:|---:|"]
        for code, value in sorted(metrics["per_class"].items(),
                                  key=lambda kv: -(kv[1]["support"] or 0)):
            lines.append(f"| {code} | {value['precision']} | {value['recall']} "
                         f"| {value['f1']} | {value['support']} | {value['predicted']} |")
        if metrics.get("unmappable_event_names"):
            lines += ["", "| 本系统词表无对应类的官方事件名 | 行数 |", "|---|---:|"]
            for name, count in list(metrics["unmappable_event_names"].items())[:15]:
                lines.append(f"| {name} | {count} |")
        return "\n".join(lines)
    if "impact_direction" in metrics:
        lines += ["| 字段 | accuracy | macro-F1 | 样本量 |", "|---|---:|---:|---:|"]
        for field in ("impact_direction", "article_sentiment"):
            value = metrics.get(field) or {}
            lines.append(f"| `{field}` | {value.get('accuracy')} | {value.get('macro_f1')} "
                         f"| {value.get('sample_size')} |")
        base = metrics.get("majority_baseline") or {}
        lines.append(f"| **恒答「{base.get('label')}」基线** | **{base.get('accuracy')}** "
                     f"| {base.get('macro_f1')} | {base.get('support')} |")
        agree = metrics.get("fields_agree") or {}
        lines += ["", f"两字段取值相同：{agree.get('count')}/{agree.get('of')}"
                      f"（{agree.get('rate')}）",
                  "", f"金标分布：{metrics.get('gold_distribution')}"]
        return "\n".join(lines)
    order = ("pair_precision", "pair_recall", "pair_f1", "b3_precision", "b3_recall", "b3_f1")
    lines += ["| 指标 | 值 |", "|---|---:|"]
    for key in order:
        if key in metrics:
            lines.append(f"| {key} | {metrics[key]} |")
    lines += ["", "| 计数 | 值 |", "|---|---:|"]
    for key in ("scored_articles", "excluded_articles", "gold_clusters", "predicted_clusters",
                "pair_gold", "pair_predicted", "pair_true_positives",
                "gold_pairs_before_exclusions", "gold_pairs_lost_to_exclusions"):
        if key in metrics:
            lines.append(f"| {key} | {int(metrics[key])} |")
    if metrics.get("exclusion_reasons"):
        lines += ["", "| 未参评原因 | 篇数 |", "|---|---:|"]
        for reason, count in sorted(metrics["exclusion_reasons"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {reason} | {count} |")
    if metrics.get("representative"):
        rep = metrics["representative"]
        lines += ["", f"代表文档一致率：{rep['rate']}"
                      f"（可比簇 {rep['comparable_clusters']}，一致 {rep['agreed']}）"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    extractor = sub.add_parser("extract", help="unpack this track's files from the archive")
    extractor.add_argument("--zip", type=Path, default=DATASET_ZIP)
    extractor.add_argument("--target", type=Path, default=GOLD_DIR)
    extractor.add_argument("--include-pool", action="store_true",
                           help="also unpack the 340MB unlabelled pools")

    importer = sub.add_parser("import", help="load the labelled gold into the benchmark database")
    importer.add_argument("--split", action="append", choices=SPLITS,
                          help="repeatable; default is both")
    importer.add_argument("--source", type=Path, default=GOLD_DIR)
    importer.add_argument("--db", type=Path, default=BENCHMARK_DB)

    stats = sub.add_parser("stats", help="what is loaded, and whether it is coherent")
    stats.add_argument("--db", type=Path, default=BENCHMARK_DB)

    runner = sub.add_parser("run", help="score a task against the gold")
    runner.add_argument("--task", choices=("e5", "e3", "e1"), required=True)
    runner.add_argument("--layer", choices=tuple(("issue_codes", "event_types")),
                        default="issue_codes", help="E1 only: which vocabulary")
    runner.add_argument("--split", choices=SPLITS, required=True)
    runner.add_argument("--params-from", help="reuse the winning parameters of a sweep run")
    runner.add_argument("--limit", type=int, help="cap the number of articles scored")
    runner.add_argument("--title-threshold", type=float)
    runner.add_argument("--body-threshold", type=float)
    runner.add_argument("--hamming-max", type=int)
    runner.add_argument("--bands", type=int)
    runner.add_argument("--containment-threshold", type=float)
    runner.add_argument("--max-reprint-gap-days", type=int)
    runner.add_argument("--template-text-bar", type=float)
    runner.add_argument("--template-number-bar", type=float)
    runner.add_argument("--template-number-share", type=float)
    runner.add_argument("--source", type=Path, default=GOLD_DIR)
    runner.add_argument("--db", type=Path, default=BENCHMARK_DB)

    sweeper = sub.add_parser("sweep", help="grid-search a gate on the train split")
    sweeper.add_argument("--task", choices=("e5",), required=True)
    sweeper.add_argument("--split", choices=SPLITS, default="train")
    sweeper.add_argument("--grid", help="JSON object of parameter -> list of values")
    sweeper.add_argument("--source", type=Path, default=GOLD_DIR)
    sweeper.add_argument("--db", type=Path, default=BENCHMARK_DB)

    profiler = sub.add_parser("profile", help="describe a constant the gold cannot calibrate")
    profiler.add_argument("--what", choices=("repost-similarity",), required=True)
    profiler.add_argument("--split", choices=SPLITS, default="train")
    profiler.add_argument("--db", type=Path, default=BENCHMARK_DB)

    reporter = sub.add_parser("report", help="render recorded metrics")
    reporter.add_argument("--run", help="a run id; default is the newest of --task")
    reporter.add_argument("--task", default="e5")
    reporter.add_argument("--format", choices=("json", "md"), default="json")
    reporter.add_argument("--db", type=Path, default=BENCHMARK_DB)

    args = parser.parse_args(argv)

    if args.command == "extract":
        written = extract(args.zip, args.target, include_pool=args.include_pool)
        for path in written:
            print(f"{path.stat().st_size:>12,}  {path}")
        return 0

    db = connect(args.db)
    ensure_schema(db)
    if args.command == "import":
        splits = args.split or list(SPLITS)
        report: dict[str, Any] = {"dataset_version": dataset_version(args.source),
                                  "checksums": file_checksums(args.source), "splits": {}}
        report["taxonomy"] = import_taxonomy(db, args.source)
        for split in splits:
            report["splits"][split] = import_split(db, split, args.source)
        report["integrity"] = integrity(db)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "stats":
        print(json.dumps(summarise(db), ensure_ascii=False, indent=2))
    elif args.command == "run":
        if args.task == "e1":
            run_id, scores = run_e1(db, args.split, args.layer, source=args.source)
            scores.pop("predictions", None)
            print(json.dumps({"run_id": run_id, "scores": scores}, ensure_ascii=False, indent=2))
            db.close()
            return 0
        if args.task == "e3":
            run_id, scores = run_e3(db, args.split, args.limit, source=args.source)
            scores.pop("predictions", None)
            print(json.dumps({"run_id": run_id, "scores": scores}, ensure_ascii=False, indent=2))
            db.close()
            return 0
        params = {}
        if args.params_from:
            params = winning_params(db, args.params_from)
        for key in ("title_threshold", "body_threshold", "hamming_max", "bands",
                    "containment_threshold", "max_reprint_gap_days",
                    "template_text_bar", "template_number_bar", "template_number_share"):
            value = getattr(args, key, None)
            if value is not None:
                params[key] = value
        run_id, scores = run_e5(db, args.split, params, source=args.source)
        print(json.dumps({"run_id": run_id, "params": {**DEFAULT_E5_PARAMS, **params},
                          "scores": scores}, ensure_ascii=False, indent=2))
    elif args.command == "sweep":
        grid = json.loads(args.grid) if args.grid else DEFAULT_E5_GRID
        run_id, results = sweep_e5(db, args.split, grid, source=args.source)
        best = max(results, key=lambda item: item["pair_f1"])
        print(json.dumps({"run_id": run_id, "combinations": len(results),
                          "best_by_pair_f1": best}, ensure_ascii=False, indent=2))
    elif args.command == "profile":
        print(json.dumps(reprint_similarity_profile(db, args.split),
                         ensure_ascii=False, indent=2))
    elif args.command == "report":
        print(render_report(db, run_id=args.run, task=args.task, fmt=args.format))
    db.close()
    return 0



# ---------------------------------------------------------------------------
# E5: near-duplicate clustering against the dataset's duplication_id gold.
#
# The gold's topology is a star -- a main-sheet article lists the ids that
# duplicate it -- which is the same shape the clustering code produces, so the
# two are directly comparable without any mapping layer.
#
# Only the three tables clustering actually writes are created here.  Calling
# pipeline.ensure_schema() would also create event_records and event_claims, and
# the guarantee that this harness cannot touch the adjudication layer is worth
# more than the reuse.
CLUSTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_documents(
    version_id TEXT PRIMARY KEY,
    near_duplicate_cluster_id TEXT);
CREATE TABLE IF NOT EXISTS duplicate_clusters(
    cluster_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, cluster_kind TEXT NOT NULL,
    representative_version_id TEXT NOT NULL, member_count INTEGER NOT NULL,
    similarity_threshold REAL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS duplicate_cluster_members(
    cluster_id TEXT NOT NULL, version_id TEXT NOT NULL, similarity REAL NOT NULL,
    PRIMARY KEY(cluster_id, version_id));
"""

# Clustering drops these silently.  Reproduced here so that every dropped row is
# named in benchmark_exclusions rather than quietly shrinking a denominator.
MIN_CLUSTER_TEXT_CHARS = 100
MIN_CLUSTER_TOKENS = 8

DEFAULT_E5_PARAMS = {
    "title_threshold": pipeline.TITLE_MATCH_THRESHOLD,
    "body_threshold": pipeline.BODY_ONLY_THRESHOLD,
    "hamming_max": pipeline.SIMHASH_HAMMING_MAX,
    "bands": pipeline.SIMHASH_BANDS,
    "containment_threshold": pipeline.TITLE_CONTAINMENT_THRESHOLD,
    "max_reprint_gap_days": pipeline.REPRINT_GAP_DAYS,
    "template_text_bar": pipeline.TEMPLATE_TEXT_BAR,
    "template_number_bar": pipeline.TEMPLATE_NUMBER_BAR,
    "template_number_share": pipeline.TEMPLATE_NUMBER_SHARE,
}


def ensure_cluster_schema(db: sqlite3.Connection) -> None:
    db.executescript(CLUSTER_SCHEMA)
    db.commit()


def code_revision() -> str | None:
    """Short git revision, so a metric can be traced to the code that made it."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def start_run(db: sqlite3.Connection, task: str, split: str, params: dict[str, Any],
              *, source: Path = GOLD_DIR, map_version: str | None = None,
              sample_cap: int | None = None, notes: str | None = None) -> str:
    version = dataset_version(source)
    started = utc_now()
    run_id = hashlib.sha256(
        f"{task}\0{split}\0{version}\0{json.dumps(params, sort_keys=True)}\0{started}".encode()
    ).hexdigest()[:16]
    db.execute("""INSERT OR REPLACE INTO benchmark_runs
        (run_id,task,split,dataset_version,map_version,params_json,code_rev,model_name,
         prompt_version,sample_cap,started_at,completed_at,status,notes)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, task, split, version, map_version, json.dumps(params, sort_keys=True),
         code_revision(), None, None, sample_cap, started, None, "running", notes))
    db.commit()
    return run_id


def finish_run(db: sqlite3.Connection, run_id: str, status: str = "ok") -> None:
    db.execute("UPDATE benchmark_runs SET completed_at=?, status=? WHERE run_id=?",
               (utc_now(), status, run_id))
    db.commit()


def record_metric(db: sqlite3.Connection, run_id: str, task: str, metric: str,
                  value: float | None, support: int | None = None,
                  detail: Any = None) -> None:
    db.execute("""INSERT OR REPLACE INTO benchmark_metrics
        (run_id,task,metric,value,support,detail_json) VALUES(?,?,?,?,?,?)""",
        (run_id, task, metric, value, support,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None))


def gold_documents(db: sqlite3.Connection, split: str) -> tuple[
        list[tuple[pipeline.RawDocument, str]], dict[str, str], dict[str, str]]:
    """Gold rows of one split shaped for the clustering code.

    Returns the candidates, the version-id -> article-id map, and the rows that
    cannot be scored with the reason for each.

    Duplicate-sheet rows carry no entity of their own, so they inherit the
    entity of the article they duplicate.  A duplicate whose parent sits in the
    other split cannot inherit anything without leaking across the split, so it
    is excluded and counted -- 1,431 of the dataset's links cross the boundary,
    which is a property of the gold, not of the system under test.
    """
    inherited: dict[str, str] = {}
    for row in db.execute("""SELECT l.child_article_id AS child, p.entity_id AS entity
        FROM gold_duplicate_links l
        JOIN gold_articles c ON c.article_file_id = l.child_article_id
        JOIN gold_articles p ON p.article_file_id = l.parent_article_id
        WHERE c.split=? AND p.split=? AND p.entity_id IS NOT NULL""", (split, split)):
        inherited.setdefault(row["child"], row["entity"])

    candidates: list[tuple[pipeline.RawDocument, str]] = []
    version_to_article: dict[str, str] = {}
    excluded: dict[str, str] = {}
    for row in db.execute("""SELECT a.*, t.text FROM gold_articles a
        JOIN gold_article_text t ON t.article_file_id = a.article_file_id
        WHERE a.split=? ORDER BY a.article_file_id""", (split,)):
        article_id = row["article_file_id"]
        entity = row["entity_id"] or inherited.get(article_id)
        text = row["text"] or ""
        if not entity:
            excluded[article_id] = "no_entity_in_this_split"
            continue
        if len(text) < MIN_CLUSTER_TEXT_CHARS:
            excluded[article_id] = "text_below_cluster_floor"
            continue
        if len(pipeline.tokenize(f"{row['title'] or ''}\n{text[:12000]}")) < MIN_CLUSTER_TOKENS:
            excluded[article_id] = "too_few_tokens"
            continue
        version_id = row["version_id"]
        version_to_article[version_id] = article_id
        candidates.append((pipeline.RawDocument(
            version_id=version_id, source_id=row["article_source"] or "unknown",
            source_tier="S3", company_id=entity, title=row["title"],
            published_at=row["published_at"], canonical_url="",
            document_type="dataset_news", content_hash=row["content_hash"],
            raw_path="", review_status="gold"), text))
    return candidates, version_to_article, excluded


def gold_clusters(db: sqlite3.Connection, split: str) -> dict[str, set[str]]:
    """Gold partition of one split, as connected components over the links.

    Components rather than raw stars because one train child names two different
    parents; merging that component is the only reading that keeps the gold a
    partition, and the anomaly is reported separately.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[a] = b

    for row in db.execute("""SELECT l.child_article_id AS child, l.parent_article_id AS parent
        FROM gold_duplicate_links l
        JOIN gold_articles c ON c.article_file_id = l.child_article_id
        JOIN gold_articles p ON p.article_file_id = l.parent_article_id
        WHERE c.split=? AND p.split=?""", (split, split)):
        union(row["child"], row["parent"])

    clusters: dict[str, set[str]] = {}
    for node in list(parent):
        clusters.setdefault(find(node), set()).add(node)
    return {key: members for key, members in clusters.items() if len(members) > 1}


def predicted_clusters(db: sqlite3.Connection,
                       version_to_article: dict[str, str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in db.execute("SELECT cluster_id, version_id FROM duplicate_cluster_members"):
        article = version_to_article.get(row["version_id"])
        if article:
            out.setdefault(row["cluster_id"], set()).add(article)
    return {key: members for key, members in out.items() if len(members) > 1}


def _pairs(clusters: dict[str, set[str]], universe: set[str]) -> set[frozenset[str]]:
    out: set[frozenset[str]] = set()
    for members in clusters.values():
        inside = sorted(members & universe)
        for i, left in enumerate(inside):
            for right in inside[i + 1:]:
                out.add(frozenset((left, right)))
    return out


def pair_scores(gold: set[frozenset[str]], predicted: set[frozenset[str]]) -> dict[str, float]:
    hit = len(gold & predicted)
    precision = hit / len(predicted) if predicted else 0.0
    recall = hit / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else 0.0
    return {"pair_precision": round(precision, 4), "pair_recall": round(recall, 4),
            "pair_f1": round(f1, 4), "pair_true_positives": hit,
            "pair_predicted": len(predicted), "pair_gold": len(gold)}


def _assignment(clusters: dict[str, set[str]], universe: set[str]) -> dict[str, set[str]]:
    """item -> the set it shares a cluster with, singletons included."""
    out: dict[str, set[str]] = {}
    for members in clusters.values():
        inside = members & universe
        for item in inside:
            out[item] = inside
    for item in universe:
        out.setdefault(item, {item})
    return out


def b3_scores(gold: dict[str, set[str]], predicted: dict[str, set[str]],
              universe: set[str]) -> dict[str, float]:
    """B-cubed, which unlike pair counting does not reward one huge cluster.

    Representative-agnostic on purpose: our representative is the earliest
    publication and the gold's is whichever row the organiser put in the main
    sheet, and those need not be the same document.
    """
    gold_of = _assignment(gold, universe)
    pred_of = _assignment(predicted, universe)
    if not universe:
        return {"b3_precision": 0.0, "b3_recall": 0.0, "b3_f1": 0.0}
    precision = sum(len(gold_of[i] & pred_of[i]) / len(pred_of[i]) for i in universe) / len(universe)
    recall = sum(len(gold_of[i] & pred_of[i]) / len(gold_of[i]) for i in universe) / len(universe)
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else 0.0
    return {"b3_precision": round(precision, 4), "b3_recall": round(recall, 4),
            "b3_f1": round(f1, 4)}


def representative_agreement(db: sqlite3.Connection, gold: dict[str, set[str]],
                             predicted: dict[str, set[str]],
                             version_to_article: dict[str, str]) -> dict[str, Any]:
    """How often our earliest-publication representative is the gold's parent.

    Only scored over gold clusters that name exactly one main-sheet article, so
    a merged component with two parents is not counted either way.
    """
    labelled = {row["article_file_id"] for row in db.execute(
        f"SELECT article_file_id FROM gold_articles WHERE sheet IN "
        f"({','.join('?' * len(LABELLED_SHEETS))})", LABELLED_SHEETS)}
    ours: dict[str, str] = {}
    for row in db.execute("SELECT cluster_id, representative_version_id FROM duplicate_clusters"):
        article = version_to_article.get(row["representative_version_id"])
        if article:
            ours[row["cluster_id"]] = article
    comparable = agreed = 0
    for gold_members in gold.values():
        parents = gold_members & labelled
        if len(parents) != 1:
            continue
        gold_parent = next(iter(parents))
        # The predicted cluster that overlaps this gold cluster most.
        best_id, best_overlap = None, 0
        for cluster_id, members in predicted.items():
            overlap = len(members & gold_members)
            if overlap > best_overlap:
                best_id, best_overlap = cluster_id, overlap
        if best_id is None:
            continue
        comparable += 1
        if ours.get(best_id) == gold_parent:
            agreed += 1
    return {"comparable_clusters": comparable, "agreed": agreed,
            "rate": round(agreed / comparable, 4) if comparable else None}


def cluster_tokens(candidates: list[tuple[pipeline.RawDocument, str]]) -> dict[str, set[str]]:
    """Tokenise once, exactly as ``build_duplicate_clusters`` would.

    Kept next to the caller that needs it rather than inside the clustering code,
    so the shipped path stays a single self-contained function.
    """
    return {doc.version_id: pipeline.tokenize(f"{doc.title or ''}\n{text[:12000]}")
            for doc, text in candidates}


def evaluate_e5(db: sqlite3.Connection, split: str, params: dict[str, Any],
                prepared: tuple | None = None,
                tokens: dict[str, set[str]] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Cluster one split's gold and score it.  Pure measurement, no run record.

    ``prepared`` and ``tokens`` let a sweep do the expensive work once instead of
    once per grid point: tokenising the split takes over a minute and does not
    depend on any of the gates being swept.
    """
    ensure_cluster_schema(db)
    candidates, version_to_article, excluded = prepared or gold_documents(db, split)
    pipeline.build_duplicate_clusters(db, candidates, tokens_by_version=tokens, **params)
    universe = set(version_to_article.values())
    gold = gold_clusters(db, split)
    predicted = predicted_clusters(db, version_to_article)
    gold_pairs_all = _pairs(gold, set().union(*gold.values()) if gold else set())
    scores = pair_scores(_pairs(gold, universe), _pairs(predicted, universe))
    scores.update(b3_scores(gold, predicted, universe))
    scores["representative"] = representative_agreement(db, gold, predicted, version_to_article)
    scores["scored_articles"] = len(universe)
    scores["excluded_articles"] = len(excluded)
    scores["gold_clusters"] = len(gold)
    scores["predicted_clusters"] = len(predicted)
    scores["gold_pairs_before_exclusions"] = len(gold_pairs_all)
    scores["gold_pairs_lost_to_exclusions"] = len(gold_pairs_all) - scores["pair_gold"]
    scores["exclusion_reasons"] = dict(Counter(excluded.values()))
    return scores, excluded


def run_e5(db: sqlite3.Connection, split: str, params: dict[str, Any] | None = None,
           *, source: Path = GOLD_DIR) -> tuple[str, dict[str, Any]]:
    settings = dict(DEFAULT_E5_PARAMS)
    settings.update(params or {})
    run_id = start_run(db, "e5", split, settings, source=source)
    scores, excluded = evaluate_e5(db, split, settings)
    for key, value in scores.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            record_metric(db, run_id, "e5", key, float(value))
        else:
            record_metric(db, run_id, "e5", key, None, detail=value)
    for article_id, reason in excluded.items():
        db.execute("""INSERT OR REPLACE INTO benchmark_exclusions
            (run_id,article_file_id,reason) VALUES(?,?,?)""", (run_id, article_id, reason))
    finish_run(db, run_id)
    return run_id, scores


def sweep_e5(db: sqlite3.Connection, split: str, grid: dict[str, list[Any]],
             *, source: Path = GOLD_DIR) -> tuple[str, list[dict[str, Any]]]:
    """Grid-search the near-duplicate gate.  Train only, enforced here.

    A threshold chosen on the split it is then reported on is not a measurement,
    and an honour-system rule about that is one distracted afternoon away from
    being broken.  So the split is checked in code.
    """
    if split != "train":
        raise ValueError(
            "阈值扫描只在 train 上跑；test 只在参数冻结后花一次 "
            "(threshold sweeps run on the train split only)")
    keys = sorted(grid)
    combos = [dict(zip(keys, values)) for values in product(*(grid[key] for key in keys))]
    run_id = start_run(db, "e5-sweep", split, {"grid": grid}, source=source,
                       notes=f"{len(combos)} parameter combinations")
    prepared = gold_documents(db, split)
    tokens = cluster_tokens(prepared[0])
    results = []
    for combo in combos:
        settings = dict(DEFAULT_E5_PARAMS)
        settings.update(combo)
        scores, _ = evaluate_e5(db, split, settings, prepared, tokens)
        results.append({"params": combo,
                        **{key: scores[key] for key in
                           ("pair_precision", "pair_recall", "pair_f1",
                            "b3_precision", "b3_recall", "b3_f1")}})
        # Checkpointed per grid point.  A full grid takes about an hour; writing
        # only at the end means a crash at minute 59 costs the whole hour, and
        # leaves no way to see how far a running sweep has got.
        best = max(results, key=lambda item: item["pair_f1"])
        record_metric(db, run_id, "e5-sweep", "sweep", None, support=len(combos),
                      detail={"completed": len(results), "of": len(combos),
                              "results": results, "best_by_pair_f1": best,
                              "shipped": DEFAULT_E5_PARAMS})
        db.commit()
    finish_run(db, run_id)
    return run_id, results


def reprint_similarity_profile(db: sqlite3.Connection, split: str) -> dict[str, Any]:
    """Where REPOST_SIMILARITY sits in the distribution of confirmed reprints.

    This is a characterisation, not a calibration: the gold says which articles
    duplicate which, never which of them count as 转载 rather than 改写, so the
    0.90 boundary cannot be fitted -- only described.
    """
    candidates, version_to_article, _ = gold_documents(db, split)
    texts = {doc.version_id: (doc, text) for doc, text in candidates}
    by_article = {article: version for version, article in version_to_article.items()}
    similarities: list[float] = []
    for members in gold_clusters(db, split).values():
        inside = sorted(m for m in members if m in by_article)
        for i, left in enumerate(inside):
            for right in inside[i + 1:]:
                left_doc, left_text = texts[by_article[left]]
                right_doc, right_text = texts[by_article[right]]
                a = pipeline.tokenize(f"{left_doc.title or ''}\n{left_text[:12000]}")
                b = pipeline.tokenize(f"{right_doc.title or ''}\n{right_text[:12000]}")
                union = len(a | b)
                similarities.append(len(a & b) / union if union else 0.0)
    similarities.sort()
    if not similarities:
        return {"pairs": 0}

    def percentile(share: float) -> float:
        index = min(len(similarities) - 1, int(share * len(similarities)))
        return round(similarities[index], 4)

    threshold = propagation.REPOST_SIMILARITY
    below = sum(1 for value in similarities if value < threshold)
    return {"pairs": len(similarities),
            "p10": percentile(0.10), "p25": percentile(0.25), "median": percentile(0.50),
            "p75": percentile(0.75), "p90": percentile(0.90),
            "repost_similarity": threshold,
            "confirmed_duplicate_pairs_below_it": below,
            "share_below": round(below / len(similarities), 4)}

# ---------------------------------------------------------------------------
# E3: article polarity against the dataset's event_emotion.
#
# The gold is entity-relative -- it states the polarity of the event *for the
# labelled entity*, and the dataset contains articles written about one company
# but labelled for another.  That is ``impact_direction`` in this system, not
# ``article_sentiment``.  Both are scored, because the pair of numbers is the
# evidence for which field the dataset is actually talking about, and because
# ``article_sentiment`` is a bare enum that the extraction prompt never defines.
EMOTION_OF_DIRECTION = {"positive": "正面", "neutral": "中性", "negative": "负面"}
GOLD_EMOTIONS = ("正面", "中性", "负面")
SENTIMENT_FIELDS = ("impact_direction", "article_sentiment")


def labelled_documents(db: sqlite3.Connection, split: str,
                       limit: int | None = None) -> list[tuple[pipeline.RawDocument, str, dict]]:
    """Labelled rows of one split, shaped for the rule labeller."""
    sql = f"""SELECT a.*, t.text, l.event_name, l.event_emotion
        FROM gold_articles a
        JOIN gold_article_text t ON t.article_file_id = a.article_file_id
        JOIN gold_event_labels l ON l.article_file_id = a.article_file_id
        WHERE a.split=? AND a.sheet IN ({','.join('?' * len(LABELLED_SHEETS))})
        ORDER BY a.article_file_id"""
    params: list[Any] = [split, *LABELLED_SHEETS]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    out = []
    for row in db.execute(sql, params):
        out.append((pipeline.RawDocument(
            version_id=row["version_id"], source_id=row["article_source"] or "unknown",
            source_tier="S3", company_id=row["entity_id"], title=row["title"],
            published_at=row["published_at"], canonical_url="",
            document_type="dataset_news", content_hash=row["content_hash"],
            raw_path="", review_status="gold"), row["text"] or "", dict(row)))
    return out


def majority_baseline(truths: list[str]) -> dict[str, Any]:
    """Always answering the commonest class.

    Reported beside every polarity number because the gold is 90% 正面: an
    accuracy below this line is worse than a constant, and a macro-F1 shows that
    where accuracy does not.
    """
    if not truths:
        return {"label": None, "accuracy": None, "macro_f1": None}
    # Ties are broken by label rather than by insertion order: on a stratified
    # draw two classes really can tie, and a baseline that changes name between
    # runs is not a baseline.
    counts = Counter(truths)
    label = min(counts, key=lambda name: (-counts[name], name))
    count = counts[label]
    scores = class_scores([(label, truth) for truth in truths])
    return {"label": label, "accuracy": round(count / len(truths), 4),
            "macro_f1": scores["macro_f1"], "support": len(truths)}


def evaluate_e3(db: sqlite3.Connection, split: str,
                limit: int | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Score the deterministic label path's polarity against event_emotion."""
    documents = labelled_documents(db, split, limit)
    excluded: dict[str, str] = {}
    truths: list[str] = []
    predictions: dict[str, dict[str, str]] = {field: {} for field in SENTIMENT_FIELDS}
    per_article: dict[str, dict[str, Any]] = {}
    for doc, text, row in documents:
        article_id = row["article_file_id"]
        gold = row["event_emotion"]
        if gold not in GOLD_EMOTIONS:
            excluded[article_id] = "gold_emotion_outside_vocabulary"
            continue
        label = pipeline.rule_label(doc, text)
        if label["eventhood"] == "IRRELEVANT":
            excluded[article_id] = "rule_path_found_no_linked_entity_or_text"
            continue
        truths.append(gold)
        per_article[article_id] = {"gold": gold}
        for field in SENTIMENT_FIELDS:
            said = EMOTION_OF_DIRECTION.get(label.get(field), "中性")
            predictions[field][article_id] = said
            per_article[article_id][field] = said

    scores: dict[str, Any] = {"scored_articles": len(truths),
                              "excluded_articles": len(excluded),
                              "exclusion_reasons": dict(Counter(excluded.values())),
                              "gold_distribution": dict(Counter(truths)),
                              "majority_baseline": majority_baseline(truths)}
    ordered = sorted(per_article)
    for field in SENTIMENT_FIELDS:
        pairs = [(predictions[field][article], per_article[article]["gold"])
                 for article in ordered]
        result = class_scores(pairs)
        correct = sum(1 for said, truth in pairs if said == truth)
        scores[field] = {"accuracy": round(correct / len(pairs), 4) if pairs else None,
                         "macro_f1": result["macro_f1"], "per_class": result["per_class"],
                         "confusion": result["confusion"], "sample_size": len(pairs)}
    # Whether the two fields are the same number.  In the deterministic path
    # article_sentiment is assigned from impact_direction, so an identical pair
    # of scores is the expected result and the point being demonstrated.
    same = sum(1 for article in ordered
               if per_article[article]["impact_direction"] == per_article[article]["article_sentiment"])
    scores["fields_agree"] = {"count": same, "of": len(ordered),
                              "rate": round(same / len(ordered), 4) if ordered else None}
    scores["predictions"] = per_article
    return scores, excluded


def run_e3(db: sqlite3.Connection, split: str, limit: int | None = None,
           *, source: Path = GOLD_DIR) -> tuple[str, dict[str, Any]]:
    run_id = start_run(db, "e3-rule", split, {"label_path": "rule", "limit": limit},
                       source=source, sample_cap=limit)
    scores, excluded = evaluate_e3(db, split, limit)
    predictions = scores.pop("predictions")
    for article_id, values in predictions.items():
        db.execute("""INSERT OR REPLACE INTO benchmark_predictions
            (run_id,article_file_id,task,predicted_label,predicted_json,latency_ms)
            VALUES(?,?,?,?,?,?)""",
            (run_id, article_id, "e3-rule", values["article_sentiment"],
             json.dumps(values, ensure_ascii=False), None))
    for key, value in scores.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            record_metric(db, run_id, "e3-rule", key, float(value))
        else:
            record_metric(db, run_id, "e3-rule", key, None, detail=value)
    for article_id, reason in excluded.items():
        db.execute("""INSERT OR REPLACE INTO benchmark_exclusions
            (run_id,article_file_id,reason) VALUES(?,?,?)""", (run_id, article_id, reason))
    finish_run(db, run_id)
    scores["predictions"] = predictions
    return run_id, scores


# ---------------------------------------------------------------------------
# E1: issue / event-type classification against the mapped official event_name.
#
# Multi-label, because both of this system's vocabularies are multi-label
# (event_types maxItems 4, issue_codes maxItems 6) and §11.2 states E1 in
# micro-F1 and macro-F1.  class_scores is single-label and is deliberately not
# reused here; folding a multi-label task into it would silently score only the
# first code.
#
# The mapping from the official event names is the one subjective step in the
# whole benchmark, so it is a frozen file rather than a dict in this module, and
# its version and hash travel with every metric.
TAXONOMY_MAP_PATH = ROOT / "config" / "event_taxonomy_map.yaml"
E1_LAYERS = {
    "issue_codes": ("event_name_to_issue_codes", "industry_event_name_to_issue_codes"),
    "event_types": ("event_name_to_event_types", "industry_event_name_to_event_types"),
}


def load_taxonomy_map(path: Path = TAXONOMY_MAP_PATH) -> dict[str, Any]:
    import yaml
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    payload["_file_sha256"] = digest
    payload["_version_label"] = f"v{payload['version']}+{digest[:8]}"
    return payload


def mapped_codes(taxonomy: dict[str, Any], layer: str, sheet: str,
                 event_name: str) -> list[str] | None:
    """The system codes an official event name folds into.

    ``None`` means the name is absent from the map, which is a broken map rather
    than an unmappable class; ``[]`` means the map says this system's vocabulary
    has no corresponding class.
    """
    company_key, industry_key = E1_LAYERS[layer]
    table = taxonomy[industry_key if sheet == INDUSTRY_SHEET else company_key]
    return table.get(event_name)


def multilabel_scores(pairs: list[tuple[set[str], set[str]]]) -> dict[str, Any]:
    """Micro and macro P/R/F1 over (predicted set, gold set) pairs.

    Micro answers "of the codes we emitted, how many were right"; macro answers
    "does the rare code work at all".  A vocabulary this skewed needs both: a
    micro figure carried by two large classes says nothing about the other
    twenty.
    """
    labels = sorted({code for said, truth in pairs for code in said | truth})
    per_class: dict[str, Any] = {}
    micro_tp = micro_fp = micro_fn = 0
    for said, truth in pairs:
        micro_tp += len(said & truth)
        micro_fp += len(said - truth)
        micro_fn += len(truth - said)
    for label in labels:
        tp = sum(1 for said, truth in pairs if label in said and label in truth)
        fp = sum(1 for said, truth in pairs if label in said and label not in truth)
        fn = sum(1 for said, truth in pairs if label not in said and label in truth)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision and recall else 0.0)
        per_class[label] = {
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": round(f1, 4), "support": tp + fn, "predicted": tp + fp}
    scored = [per_class[label]["f1"] for label in labels if per_class[label]["support"]]
    micro_p = micro_tp / (micro_tp + micro_fp) if micro_tp + micro_fp else 0.0
    micro_r = micro_tp / (micro_tp + micro_fn) if micro_tp + micro_fn else 0.0
    micro_f1 = (2 * micro_p * micro_r / (micro_p + micro_r) if micro_p and micro_r else 0.0)
    exact = sum(1 for said, truth in pairs if said == truth)
    covered = sum(1 for said, truth in pairs if said & truth)
    return {"micro_precision": round(micro_p, 4), "micro_recall": round(micro_r, 4),
            "micro_f1": round(micro_f1, 4),
            "macro_f1": round(sum(scored) / len(scored), 4) if scored else None,
            "exact_set_match": round(exact / len(pairs), 4) if pairs else None,
            "any_overlap": round(covered / len(pairs), 4) if pairs else None,
            "per_class": per_class, "sample_size": len(pairs)}


def schema_candidate_baseline(db: sqlite3.Connection, split: str) -> dict[str, Any]:
    """The official task's trivial strategy: pick the entity's own candidate.

    Reported because it is the number a reader will compare against, and it
    belongs to a different task from ours -- schema-conditioned selection among
    an entity's 1-4 defined events, not classification into a fixed vocabulary.
    Stating our mapped F1 "against" it without saying so would be a category
    error, so both the deterministic accuracy and the share of rows where the
    entity offers no choice at all are given.
    """
    rows = db.execute(f"""SELECT a.entity_id, l.event_name FROM gold_event_labels l
        JOIN gold_articles a ON a.article_file_id = l.article_file_id
        WHERE a.split=? AND a.entity_id IS NOT NULL
          AND a.sheet IN ({','.join('?' * len(LABELLED_SHEETS))})""",
        (split, *LABELLED_SHEETS)).fetchall()
    schema: dict[str, list[str]] = {}
    for row in db.execute("SELECT entity_id, event_name FROM gold_event_schema ORDER BY event_name"):
        schema.setdefault(row["entity_id"], []).append(row["event_name"])
    correct = single = total = 0
    sizes: Counter = Counter()
    for row in rows:
        candidates = schema.get(row["entity_id"], [])
        if not candidates:
            continue
        total += 1
        sizes[len(candidates)] += 1
        if len(candidates) == 1:
            single += 1
        if candidates[0] == row["event_name"]:
            correct += 1
    return {"task": "schema_conditioned_event_name_selection",
            "strategy": "取该主体候选事件名中的首个（确定性）",
            "accuracy": round(correct / total, 4) if total else None,
            "single_candidate_share": round(single / total, 4) if total else None,
            "candidate_count_distribution": dict(sorted(sizes.items())),
            "sample_size": total}


def evaluate_e1(db: sqlite3.Connection, split: str, layer: str = "issue_codes",
                taxonomy: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    taxonomy = taxonomy or load_taxonomy_map()
    documents = labelled_documents(db, split)
    excluded: dict[str, str] = {}
    pairs: list[tuple[set[str], set[str]]] = []
    per_entity: dict[str, dict[str, Any]] = {}
    unmapped_names: Counter = Counter()
    predictions: dict[str, dict[str, Any]] = {}
    for doc, text, row in documents:
        article_id = row["article_file_id"]
        codes = mapped_codes(taxonomy, layer, row["sheet"], row["event_name"])
        if codes is None:
            excluded[article_id] = "event_name_absent_from_map"
            continue
        if not codes:
            excluded[article_id] = "no_corresponding_class_in_this_vocabulary"
            unmapped_names[row["event_name"]] += 1
            continue
        label = pipeline.rule_label(doc, text)
        said = set(label.get(layer) or [])
        truth = set(codes)
        pairs.append((said, truth))
        predictions[article_id] = {"gold_event_name": row["event_name"],
                                   "gold_codes": sorted(truth), "predicted": sorted(said)}
        bucket = per_entity.setdefault(row["entity_id"] or "unknown", {
            "entity_name": row["secu_abbr"] or row["industry_name"],
            "kind": row["entity_kind"], "pairs": []})
        bucket["pairs"].append((said, truth))

    scores: dict[str, Any] = {"layer": layer,
                              "map_version": taxonomy["_version_label"],
                              "scored_articles": len(pairs),
                              "excluded_articles": len(excluded),
                              "exclusion_reasons": dict(Counter(excluded.values())),
                              "unmappable_event_names": dict(unmapped_names.most_common())}
    scores.update(multilabel_scores(pairs))
    # Per-entity breakdown: 84 companies and 17 industries, none of which this
    # system was tuned on.  §21 asks for one transfer company; this is 101.
    entities = []
    for entity_id, bucket in sorted(per_entity.items()):
        entity_scores = multilabel_scores(bucket["pairs"])
        entities.append({"entity_id": entity_id, "entity_name": bucket["entity_name"],
                         "kind": bucket["kind"], "articles": len(bucket["pairs"]),
                         "micro_f1": entity_scores["micro_f1"],
                         "any_overlap": entity_scores["any_overlap"]})
    scores["per_entity"] = entities
    scores["entities_scored"] = len(entities)
    # Computed once, with a tiebreak by code name: recomputing the counter per
    # row was quadratic, and most_common() breaks ties by insertion order, which
    # would let the baseline change name between runs on the same data.
    gold_counts = Counter(code for _, truth in pairs for code in truth)
    most_frequent = (min(gold_counts, key=lambda code: (-gold_counts[code], code))
                     if gold_counts else None)
    scores["baselines"] = {
        "most_frequent_code": (
            {**multilabel_scores([({most_frequent}, truth) for _, truth in pairs]),
             "label": most_frequent} if most_frequent else None),
        "schema_candidate": schema_candidate_baseline(db, split),
    }
    if scores["baselines"]["most_frequent_code"]:
        # Keep the baseline row small; its per-class table is not informative.
        scores["baselines"]["most_frequent_code"].pop("per_class", None)
    scores["predictions"] = predictions
    return scores, excluded


def run_e1(db: sqlite3.Connection, split: str, layer: str = "issue_codes",
           *, source: Path = GOLD_DIR) -> tuple[str, dict[str, Any]]:
    taxonomy = load_taxonomy_map()
    run_id = start_run(db, f"e1-{layer}", split, {"layer": layer, "label_path": "rule"},
                       source=source, map_version=taxonomy["_version_label"])
    scores, excluded = evaluate_e1(db, split, layer, taxonomy)
    predictions = scores.pop("predictions")
    for article_id, values in predictions.items():
        db.execute("""INSERT OR REPLACE INTO benchmark_predictions
            (run_id,article_file_id,task,predicted_label,predicted_json,latency_ms)
            VALUES(?,?,?,?,?,?)""",
            (run_id, article_id, f"e1-{layer}", ",".join(values["predicted"]),
             json.dumps(values, ensure_ascii=False), None))
    for key, value in scores.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            record_metric(db, run_id, f"e1-{layer}", key, float(value))
        else:
            record_metric(db, run_id, f"e1-{layer}", key, None, detail=value)
    for article_id, reason in excluded.items():
        db.execute("""INSERT OR REPLACE INTO benchmark_exclusions
            (run_id,article_file_id,reason) VALUES(?,?,?)""", (run_id, article_id, reason))
    finish_run(db, run_id)
    scores["predictions"] = predictions
    return run_id, scores


if __name__ == "__main__":
    raise SystemExit(main())
