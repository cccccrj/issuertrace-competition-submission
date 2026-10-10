# 正式GPU实验证据

本目录仅包含完整通过的三轮真实CUDA实验、设备采样、输入和模型摘要、环境锁以及平台只读接入验收。基准源代码快照为4685ccbd5da4207f79af2977eaacdff6986adc1a；实际编码算法保持原提交实现，脚本摘要见benchmark.json。

- benchmark.json：完整参数、输入摘要、实际设备及容器配额、分段计时、显存和汇总统计。
- run-1至run-3：每轮完整近邻ID／分数、清单及GPU采样；不包含金融披露正文。
- application-acceptance.json：标准导入接口、3922条主张覆盖与1175条固定启发式检索标签复核；不属于人工事实准确率。
- input-manifest.json：93209段证据与3922条主张的冻结摘要，不含原始输入正文。
- model-provenance.json、model-reference-metadata.json：实际加载镜像权重与官方模型参考分别标注，避免混同资产格式或历史revision。
- gpu-environment.lock.txt：实际生效依赖；金融独立环境只读复用同机已有GPU库，另安装sentence-transformers，不修改另一项目环境。
- cpu-contract-tests.log：35项向量接口与验收契约回归通过。
- github-ci-source.json／log：bf4fa83扩展源码的干净CI；前端406项通过及构建成功，后端1807项中1791通过、16既有条件跳过。
- runtime-functional-acceptance.json：扩展源码配套冻结运行内容在全新隔离目录复验，65项功能、权限与协同均通过；未使用生产端口。

原输入正文、模型权重、服务器密码与供应商密钥均不随本目录分发。GPU结果在隔离目录验证，未替换冻结运行包近邻、业务裁决或在线演示环境部署。GPU计时与业务API并发延迟是独立指标。
