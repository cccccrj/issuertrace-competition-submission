#!/usr/bin/env python3
"""在平台侧只读验证GPU回传、独立目录导入和冻结标签检索效果；不替换线上产物。"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from processor import dense_index, retrieval_eval


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--work", type=Path, help="可选：不存在的私有工作目录；默认创建隔离临时目录")
    args = parser.parse_args()
    database = ROOT / "data/state/collector.sqlite3"
    gold = ROOT / "data/state/retrieval_eval/gold.jsonl"
    args.out.mkdir(parents=True, exist_ok=True)
    # install接受现有只读连接；只将产物安装至新建的私有验证目录。
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if args.work:
            args.work.mkdir(parents=True, exist_ok=False)
            work = args.work
        else:
            private = ROOT / ".private"
            private.mkdir(exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="gpu-acceptance-", dir=private))
        target = work / "installed"
        assert not target.exists(), "验收目录须为新目录，避免覆盖旧产物"
        exported = json.loads((args.bundle / "export.json").read_text())
        manifest = json.loads((args.run / "manifest.json").read_text())
        assert exported["paragraph_fingerprint"] == dense_index.paragraph_fingerprint(db)
        assert exported["sha256"] == manifest["input_sha256"]
        assert dense_index.sha256_file(args.run / "neighbors.jsonl") == manifest["neighbors_sha256"]
        # 回传测试目录只含索引ID与分数；为标准install API提供导出清单。
        bundle = work / "import-bundle"
        assert not bundle.exists()
        bundle.mkdir()
        import shutil
        for source in (args.bundle / "export.json", args.run / "manifest.json", args.run / "neighbors.jsonl"):
            shutil.copyfile(source, bundle / source.name)
        installed = dense_index.install(bundle, db=db, target=target)
        assert installed["installed"] and installed["queries"] == exported["queries"]
        loaded = dense_index.load(db, target=target)
        assert loaded is not None and len(loaded) == exported["queries"]
        expected_ids = {row[0] for row in db.execute("""SELECT c.claim_id FROM event_claims c
            JOIN event_records e ON e.event_id=c.event_id WHERE e.company_id IS NOT NULL""")}
        assert set(loaded) == expected_ids
        labels = [json.loads(line) for line in gold.read_text().splitlines() if line]
        report = retrieval_eval.evaluate(db, labels, loaded)
        assert report["sets"]["any_language"]["all"]["claims"] == 1175
        result = dict(passed=True, created_at=datetime.now(timezone.utc).isoformat(),
            input_sha256=exported["sha256"], paragraph_fingerprint=exported["paragraph_fingerprint"],
            neighbors_sha256=manifest["neighbors_sha256"], queries=len(loaded),
            imported_into_isolated_directory=True, frozen_runtime_neighbors_replaced=False,
            database_read_only=True, schema_join_coverage=True, gold_sha256=dense_index.sha256_file(gold),
            label_scope="frozen heuristic retrieval labels; not independent human factual accuracy",
            retrieval_evaluation=report)
        (args.out / "application-acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(retrieval_eval.render(report))
        print("GPU回传及平台只读接入验收通过；原冻结裁决与向量产物保持不变。")


if __name__ == "__main__":
    main()
