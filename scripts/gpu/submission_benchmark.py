#!/usr/bin/env python3
"""固定完整输入，复用项目编码／近邻实现，独立记录真实CUDA计时与验收证据。"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import threading
import time


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            value.update(block)
    return value.hexdigest()


def distribution(values):
    values = sorted(values)
    assert values
    position = (len(values) - 1) * .95
    lower, upper = math.floor(position), math.ceil(position)
    return dict(mean=round(statistics.mean(values), 6),
                median=round(statistics.median(values), 6),
                std_sample=round(statistics.stdev(values), 6) if len(values) > 1 else 0,
                p95=round(values[lower] + (values[upper] - values[lower]) * (position - lower), 6),
                min=round(values[0], 6), max=round(values[-1], 6), samples=len(values))


def validate_neighbors(rows, queries, paragraph_companies, k):
    """检查完整覆盖、公司范围、排序、有限分数与候选预算，不评判事实真假。"""
    expected = {q["id"]: q["company"] for q in queries}
    assert len(expected) == len(queries), "重复查询ID"
    counts = defaultdict(int)
    for company in paragraph_companies.values():
        counts[company] += 1
    seen, observed_scores = set(), []
    for row in rows:
        identifier = row["id"]
        assert identifier in expected and identifier not in seen
        seen.add(identifier)
        company = expected[identifier]
        hits = row["hits"]
        assert len(hits) == min(k, counts[company])
        assert len({pid for pid, _ in hits}) == len(hits)
        scores = [score for _, score in hits]
        # FP16归一与点积存在舍入误差，不修改候选分数或排序；采用明确数值容差。
        assert all(math.isfinite(v) and -1.005 <= v <= 1.005 for v in scores)
        observed_scores.extend(scores)
        assert scores == sorted(scores, reverse=True)
        assert all(paragraph_companies.get(pid) == company for pid, _ in hits)
    assert seen == set(expected)
    return dict(passed=True, queries=len(seen), all_queries_covered=True,
                company_scope_correct=True, finite_sorted_scores=True, top_k_correct=True,
                fp16_score_tolerance=.005, score_min=min(observed_scores), score_max=max(observed_scores))


def command(args):
    return subprocess.check_output(args, text=True).strip()


def sample_gpu():
    fields = [float(v.strip()) for v in command(["nvidia-smi", "--id=0",
        "--query-gpu=memory.used,utilization.gpu,power.draw,temperature.gpu",
        "--format=csv,noheader,nounits"]).split(",")]
    return dict(monotonic_s=time.perf_counter(), resident_mib=fields[0],
                utilization_pct=fields[1], power_w=fields[2], temperature_c=fields[3])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--top-k", type=int, default=20)
    args = parser.parse_args()
    assert args.repeats >= 3 and args.batch_size > 0
    assert len(args.source_commit) == 40
    import torch
    assert torch.cuda.is_available(), "正式基准需要真实CUDA设备"
    torch.manual_seed(42)
    torch.cuda.set_device(0)
    torch.backends.cuda.matmul.allow_tf32 = False
    spec = importlib.util.spec_from_file_location("issuertrace_gpu_encoder", Path(__file__).with_name("embed_neighbors.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    exported = json.loads((args.bundle / "export.json").read_text())
    reference = json.loads(args.reference_manifest.read_text())
    assert exported["sha256"] == reference["input_sha256"], "输入必须与冻结提交向量快照一致"
    assert exported["paragraph_fingerprint"] == reference["paragraph_fingerprint"]
    for name, expected in exported["sha256"].items():
        assert digest(args.bundle / name) == expected
    paragraphs = module.read_jsonl(args.bundle / "paragraphs.jsonl")
    queries = module.read_jsonl(args.bundle / "queries.jsonl")
    assert len(paragraphs) == exported["paragraphs"] == reference["paragraphs"]
    assert len(queries) == exported["queries"] == reference["queries"]
    assert len({p["id"] for p in paragraphs}) == len(paragraphs)
    p_by_company, q_by_company = defaultdict(list), defaultdict(list)
    for p in paragraphs:
        p_by_company[p["company"]].append(p)
    for q in queries:
        q_by_company[q["company"]].append(q)
    assert set(q_by_company) <= set(p_by_company)
    args.out.mkdir(parents=True, exist_ok=True)
    models = [dict(file=str(p.relative_to(args.model)), bytes=p.stat().st_size, sha256=digest(p))
              for p in sorted(args.model.rglob("*")) if p.is_file()]
    cgroup = {name: Path("/sys/fs/cgroup", name).read_text().strip()
              for name in ("cpu.max", "memory.max", "cpuset.cpus.effective")
              if Path("/sys/fs/cgroup", name).exists()}
    result = dict(started_at=datetime.now(timezone.utc).isoformat(), complete=False,
        source_commit=args.source_commit, benchmark_script_sha256=digest(__file__),
        encoder_script_sha256=digest(Path(__file__).with_name("embed_neighbors.py")),
        input=exported, model_files=models, hardware=dict(
            gpu=command(["nvidia-smi", "--id=0", "--query-gpu=name,driver_version,memory.total,power.limit",
                         "--format=csv,noheader"]), cpu=command(["lscpu"]), cgroup=cgroup,
            capability=torch.cuda.get_device_capability(0)),
        environment=dict(python=platform.python_version(), os=platform.platform(),
            torch=torch.__version__, cuda=torch.version.cuda,
            packages={name: importlib.metadata.version(name) for name in (
                "sentence-transformers", "transformers", "tokenizers", "numpy", "huggingface-hub")}),
        parameters=dict(batch_size=args.batch_size, max_length=args.max_length, top_k=args.top_k,
            precision="float16", normalized=True, device="cuda:0", actual_gpu_count=1,
            repeats=args.repeats, seed=42, allow_tf32=False), runs=[])
    def save():
        temporary = args.out / "benchmark.tmp"
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(args.out / "benchmark.json")
    save()
    start = time.perf_counter()
    encoder = module.ModelEncoder(str(args.model), args.batch_size, args.max_length, "cuda:0")
    torch.cuda.synchronize()
    result["model_load_seconds"] = round(time.perf_counter() - start, 6)
    assert encoder.model.device.type == "cuda"
    assert next(encoder.model.parameters()).dtype == torch.float16
    # 使用实际输入预热一批段落、一批查询和近邻，计时排除预热与模型加载。
    warm_p = paragraphs[:args.batch_size]
    warm_q = queries[:args.batch_size]
    pv = encoder.encode([p["text"] for p in warm_p])
    qv = encoder.encode([q["text"] for q in warm_q])
    module.topk_torch(qv, pv, [p["id"] for p in warm_p], args.top_k)
    torch.cuda.synchronize()
    del pv, qv
    result["warmup"] = dict(paragraphs=len(warm_p), queries=len(warm_q), batches=1, excluded_from_timing=True)
    paragraph_companies = {p["id"]: p["company"] for p in paragraphs}
    for repetition in range(1, args.repeats + 1):
        folder = args.out / f"run-{repetition}"
        folder.mkdir()
        samples, sampler_errors, stop = [], [], threading.Event()
        def collect():
            while not stop.is_set():
                try:
                    samples.append(sample_gpu())
                except Exception as exc:
                    sampler_errors.append(type(exc).__name__)
                stop.wait(.25)
        sampler = threading.Thread(target=collect, daemon=True)
        sampler.start()
        torch.cuda.reset_peak_memory_stats()
        all_rows, companies = [], []
        started = time.perf_counter()
        try:
            for company in sorted(q_by_company):
                pool, targets = p_by_company[company], q_by_company[company]
                times = {}
                for label, texts in (("paragraph_encode_seconds", [p["text"] for p in pool]),
                                      ("query_encode_seconds", [q["text"] for q in targets])):
                    torch.cuda.synchronize()
                    t = time.perf_counter()
                    vectors = encoder.encode(texts)
                    torch.cuda.synchronize()
                    times[label] = time.perf_counter() - t
                    assert vectors.shape[1] == 1024 and vectors.device.type == "cuda"
                    assert torch.isfinite(vectors).all().item()
                    assert (vectors.float().norm(dim=1) - 1).abs().max().item() < .01
                    if label.startswith("paragraph"):
                        paragraph_vectors = vectors
                    else:
                        query_vectors = vectors
                torch.cuda.synchronize()
                t = time.perf_counter()
                hits = module.topk_torch(query_vectors, paragraph_vectors, [p["id"] for p in pool], args.top_k)
                torch.cuda.synchronize()
                times["neighbors_seconds"] = time.perf_counter() - t
                all_rows.extend(dict(id=q["id"], hits=h) for q, h in zip(targets, hits))
                companies.append(dict(company=company, paragraphs=len(pool), queries=len(targets),
                                      **{key:round(value, 6) for key, value in times.items()}))
                del vectors, paragraph_vectors, query_vectors
            acceptance = validate_neighbors(all_rows, queries, paragraph_companies, args.top_k)
            t = time.perf_counter()
            with (folder / "neighbors.jsonl").open("w") as handle:
                for row in all_rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            write_seconds = time.perf_counter() - t
            elapsed = time.perf_counter() - started
        finally:
            stop.set()
            sampler.join(timeout=10)
        assert samples and not sampler_errors
        assert max(s["utilization_pct"] for s in samples) > 0
        encoding = sum(c["paragraph_encode_seconds"] + c["query_encode_seconds"] for c in companies)
        neighbors = sum(c["neighbors_seconds"] for c in companies)
        manifest = dict(model=encoder.name, fake=False, dimensions=1024, top_k=args.top_k,
            max_length=args.max_length, limit=None, paragraphs=len(paragraphs), queries=len(queries),
            seconds=round(elapsed, 6), input_sha256=exported["sha256"],
            neighbors_sha256=digest(folder / "neighbors.jsonl"),
            built_at=datetime.now(timezone.utc).isoformat())
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (folder / "gpu-samples.json").write_text(json.dumps(samples, indent=2) + "\n")
        result["runs"].append(dict(repetition=repetition, companies=companies,
            encoding_seconds=round(encoding, 6), neighbors_seconds=round(neighbors, 6),
            file_write_seconds=round(write_seconds, 6), elapsed_seconds=round(elapsed, 6),
            texts_per_second=round((len(paragraphs) + len(queries)) / encoding, 3),
            gpu_peak_resident_mib=max(s["resident_mib"] for s in samples),
            gpu_peak_utilization_pct=max(s["utilization_pct"] for s in samples),
            torch_peak_allocated_mib=round(torch.cuda.max_memory_allocated() / (1 << 20), 3),
            torch_peak_reserved_mib=round(torch.cuda.max_memory_reserved() / (1 << 20), 3),
            neighbors_sha256=manifest["neighbors_sha256"], acceptance=acceptance,
            gpu_sample_count=len(samples), sampler_errors=sampler_errors))
        save()
        print(f"full run {repetition}/{args.repeats} passed: {elapsed:.3f}s", flush=True)
    result.update(complete=True, finished_at=datetime.now(timezone.utc).isoformat(),
        gpu_execution_observed=True,
        summary={name: distribution([r[name] for r in result["runs"]]) for name in (
            "encoding_seconds", "neighbors_seconds", "elapsed_seconds", "texts_per_second")},
        repeat_output_hashes_identical=len({r["neighbors_sha256"] for r in result["runs"]}) == 1)
    save()
    print("IssuerTrace full GPU benchmark complete", flush=True)


if __name__ == "__main__":
    main()
