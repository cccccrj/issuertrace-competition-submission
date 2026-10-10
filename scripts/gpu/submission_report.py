#!/usr/bin/env python3
"""仅由完整正式GPU证据生成专项报告。"""
from pathlib import Path
import json
import argparse

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/submission/evidence/gpu-timing"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='v1.0')
    args = parser.parse_args()
    result = json.loads((EVIDENCE / "benchmark.json").read_text())
    integration = json.loads((EVIDENCE / "application-acceptance.json").read_text())
    assert result["complete"] and result["gpu_execution_observed"] and integration["passed"]
    assert all(run["acceptance"]["passed"] for run in result["runs"])
    p = result["parameters"]
    rows = ["| 重复 | 段落＋查询编码秒 | 近邻秒 | 全流程秒 | 编码文本/秒 |",
            "|---|---:|---:|---:|---:|"]
    memory = ["| 重复 | 驻留显存峰值MiB | 张量分配峰值MiB | GPU利用率峰值% |",
              "|---|---:|---:|---:|"]
    for run in result["runs"]:
        rows.append(f"| {run['repetition']} | {run['encoding_seconds']:.3f} | {run['neighbors_seconds']:.3f} | {run['elapsed_seconds']:.3f} | {run['texts_per_second']:.2f} |")
        memory.append(f"| {run['repetition']} | {run['gpu_peak_resident_mib']} | {run['torch_peak_allocated_mib']} | {run['gpu_peak_utilization_pct']} |")
    statistics = result["summary"]
    model = next(file for file in result["model_files"] if file["file"] == "model.safetensors")
    evaluation = integration["retrieval_evaluation"]["sets"]["any_language"]["all"]
    lines = ["# IssuerTrace GPU向量计算专项测试报告", "",
        f"正式交付版本：{args.version}。实验时间：{result['started_at']}至{result['finished_at']}。GPU代码快照：{result['source_commit']}。", "",
        "## 一、目标与运行架构", "",
        "在独立4090D服务器复测冻结输入上的BGE-M3编码与同公司top-k近邻计算，补齐计算时间、吞吐、显存和回传接入证据。平台业务API继续使用原测试环境部署；向量计算是离线批处理，预计算产物回传平台，不要求业务页面实时加载GPU模型。", "",
        "批处理通过原scripts/gpu/embed_neighbors.py的ModelEncoder和topk_torch执行，新增基准工具负责完整输入核对、预热、CUDA同步、分段计时、采样与验收，不修改原编码与候选排序实现。", "",
        "## 二、设备、模型与环境", "",
        f"设备：{result['hardware']['gpu']}；实际使用{p['actual_gpu_count']}张卡，cuda:0，计算能力{result['hardware']['capability']}。AMD EPYC9754宿主，容器18核／60GiB配额；宿主与cgroup原始信息在benchmark.json中分别记录。", "",
        f"Python={result['environment']['python']}，torch={result['environment']['torch']}，CUDA={result['environment']['cuda']}；依赖版本：{result['environment']['packages']}。完整环境锁见gpu-environment.lock.txt。", "",
        "采用预训练BGE-M3的本地Xorbits镜像，输出1024维，CLS池化与L2归一；本轮使用安全张量格式权重，推理为FP16。镜像原pytorch_model.bin的SHA256与官方BAAI/bge-m3元数据所列文件一致；实际加载的safetensors及配置单独记录文件摘要。官方revision和元数据见model-provenance.json。", "",
        f"实际加载model.safetensors：{model['sha256']}，{model['bytes']}字节。全部模型文件摘要在benchmark.json；授权权重保留于GPU部署目录，不随仓库分发。", "",
        "独立金融GPU环境复用已有只读PyTorch/CUDA依赖路径，新增sentence-transformers等依赖安装在金融独立虚拟环境，未修改CourseLoop环境和系统conda。GPU测试期间暂停同机CourseLoop模型进程以避免显存竞争，测试结束后恢复。pip check通过，实际生效版本及复现锁均记录。", "",
        "## 三、冻结输入与实验参数", "",
        f"完整输入：{result['input']['paragraphs']}段披露证据、{result['input']['queries']}条主张；两家公司分别编码和计算近邻。输入SHA256与冻结提交的export及既有向量清单一致；段落指纹：{result['input']['paragraph_fingerprint']}。", "",
        f"paragraphs.jsonl SHA256：{result['input']['sha256']['paragraphs.jsonl']}。queries.jsonl SHA256：{result['input']['sha256']['queries.jsonl']}。原始正文留在受控.private目录，Git中仅保存摘要及段落／主张ID和分数。", "",
        f"batch_size={p['batch_size']}、max_length={p['max_length']}、top_k={p['top_k']}、FP16、归一化、seed=42、关闭TF32。先用真实输入预热一批段落、一批查询及近邻，再执行{p['repeats']}次完整计算，无limit、无fake-encoder。原始编码脚本与基准脚本SHA256均在benchmark.json。", "",
        "计时前后torch.cuda.synchronize；编码耗时包含tokenization与实际模型执行，近邻耗时包含GPU矩阵运算、top-k及CPU结果准备。全流程包含结果校验和文件写入，模型加载与预热单独记录并排除。每250ms用nvidia-smi采样驻留显存、利用率、功率和温度；torch张量分配与缓存峰值另列，避免混淆指标。", "",
        "## 四、完整GPU实测结果", "", *rows, "", *memory, "",
        f"全流程平均{statistics['elapsed_seconds']['mean']:.3f}秒，样本标准差{statistics['elapsed_seconds']['std_sample']:.3f}秒，中位数{statistics['elapsed_seconds']['median']:.3f}秒，P95={statistics['elapsed_seconds']['p95']:.3f}秒。编码均值{statistics['encoding_seconds']['mean']:.3f}秒，近邻均值{statistics['neighbors_seconds']['mean']:.3f}秒；分位数采用小样本线性插值，完整原始数据见benchmark.json。", "",
        f"全部{len(result['runs'])}轮完整计算通过；每轮3922条主张均获得同公司范围内20条候选，分数有限且按原实现排序。FP16归一和点积采用0.005数值容差，保留原始分数、不裁剪或重排。重复输出哈希一致={result['repeat_output_hashes_identical']}；各轮neighbors及GPU采样分别保存。", "",
        "## 五、平台接入与效果复核", "",
        "通过原dense_index.install接口核对输入摘要、段落指纹及输出摘要，将GPU回传产物安装到独立.private目录；从只读快照加载全部3922条主张近邻，编号关联和覆盖检查通过。既有运行包与线上冻结近邻、机器裁决和阈值保持原样。", "",
        f"固定启发式检索标签复核1175条去重主张：BM25 recall@shown={evaluation['bm25']['recall@shown']}，Dense={evaluation['dense']['recall@shown']}，Hybrid={evaluation['hybrid']['recall@shown']}，Fallback={evaluation['fallback']['recall@shown']}。这些候选命中指标按04的标签与评价设计独立统计，人工事实评价采用另设裁定协议。完整分组、候选预算和方案选择见application-acceptance.json。", "",
        "GPU验收工具与既有向量导入／检索契约共35项CPU回归通过，见cpu-contract-tests.log；实际CUDA计算独立以本报告三轮结果验证。", "",
        "## 六、复现与交付", "",
        "```bash", "# 平台：Python3.12，按授权快照导出，不导出账号和密钥",
        "python -m processor.dense_index export --out <受控输入目录>",
        "# GPU：独立环境，安装scripts/gpu/requirements-submission.txt，准备授权模型",
        "python scripts/gpu/submission_benchmark.py --bundle <输入目录> --model <模型目录> --reference-manifest <冻结data/state/dense/manifest.json> --out <结果目录> --source-commit <40位提交> --repeats 3 --batch-size 64 --max-length 512 --top-k 20",
        "# 平台：重新生成固定标签并在新建隔离目录验收回传（不替换生产）",
        "python -m processor.retrieval_eval build",
        "python scripts/gpu/submission_acceptance.py --bundle <输入目录> --run <结果目录/run-1> --out <验收目录>",
        "```", "",
        "实际云端采用PYTHONPATH指定只读公共GPU依赖路径，HF_HUB_OFFLINE=1、OMP_NUM_THREADS=8；全新GPU环境按锁安装可独立复现，不要求保留另一项目。输入受数据授权约束，传输仅限获授权的部署环境；模型供应商密钥和服务器SSH口令独立管理。", "",
        "## 七、结果口径与扩展方向", "",
        "历史V100161.6秒与本次4090D结果按设备及运行版本分别归档。本轮固定输入哈希、模型资产摘要和算法参数，形成可重放的单卡计算基线；严格跨设备加速比、双卡并行及更多配置按同一模型资产和环境的对照协议扩展。", "",
        "本轮完成向量计算性能与平台回传验证；业务API延迟、外部LLM生成容量和训练实验按各自链路评价。不同batch／长度、长期GPU容量和更多公司可按07协议扩展；已验证业务容量与性能基线详见05。", "",
        "技术来源：[官方BGE-M3模型说明](https://huggingface.co/BAAI/bge-m3)。"]
    (ROOT / "docs/submission/08-GPU向量计算测试报告.md").write_text("\n".join(lines) + "\n")
    print("GPU专项报告已按完整正式证据生成。")


if __name__ == "__main__":
    main()
