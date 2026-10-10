# 上市公司事件智识 · IssuerTrace 比赛提交版

从多源财经信息到事件、证据核验、风险事项与人工协同的可运行原型。机器判断保留依据，人工结论独立留痕；本作品不提供投资建议。

本仓库为比赛独立提交版 V1.0，包含应用源码、正式报告及可复现部署材料。正式交付口径见 [交付索引](docs/submission/00-交付索引.md)。数据、算法和实验口径统一收录于正式报告，配套证据支持复核。

## 立即访问

本地演示入口：`http://127.0.0.1:8000/`，部署步骤见下文。线上体验地址与评审凭据通过比赛指定渠道单独提供。

线上评审凭据通过比赛指定渠道单独提供；本地多角色演示账号见 [ACCESS.md](ACCESS.md)。在线演示用于功能体验，写入／协同压测使用独立测试数据库。

| 环境 | 版本与数据 | 用途 |
|---|---|---|
| 在线演示原型 | 核心功能演示；独立评审身份 | 在线体验与证据核验 |
| 交付运行包 | `v1.0`；脱敏业务快照、独立评测库及预计算近邻 | 本地部署、协同与已有测试复现 |
| 4090D离线实验 | GPU代码快照 `4685ccbd`；93209段落／3922查询 | 固定输入上的GPU计算计时及回传验证 |
| 业务验收环境 | 独立测试环境实例；实测快照标识见测试证据 | 功能、权限、协同及持续负载验证 |

本次交付保持线上服务不变。运行包提供完整处理后业务与评测快照，数据组成见[数据范围](docs/submission/处理后数据交付范围.md)。

## 本地启动

Python3.12、Linux推荐，无需GPU。以下部署v1.0：检出对应源码标签，再将运行包解压至该源码目录。`gh`需要已获本仓库权限的GitHub账号登录。

```bash
git clone https://github.com/cccccrj/issuertrace-competition-submission.git
cd issuertrace-competition-submission
git checkout v1.0
gh release download v1.0 --repo cccccrj/issuertrace-competition-submission \
  --pattern 'issuertrace-runtime-v1.0.tar.gz' --pattern SHA256SUMS
sha256sum --ignore-missing -c SHA256SUMS
tar xzf issuertrace-runtime-v1.0.tar.gz
python3.12 -m venv .venv
.venv/bin/python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-submission.lock.txt
bash scripts/submission_start.sh
```

打开 http://127.0.0.1:8000。包内已有前端构建产物、业务快照与评测库，可直接运行查询、阅读、导出与协同；配置模型网关后启用问答。部署实例的签名密钥和模型API配置独立管理。

## 源码结构与测试

```text
api/          FastAPI、权限、只读查询、人工协同、通知、助手、报告导出
collector/    多源采集与原始版本留存
processor/    抽取、检索、核验、聚类、关系、离线算法评测
config/       公司、来源、词库与规则配置
web/          Vue3前端、组件测试、浏览器体检
tests/        Python单元与集成测试（合成夹具，不依赖生产账号）
scripts/      部署、HTTP验收、持续负载、数据清理与报告生成
docs/submission/ 正式报告、案例与原始验收证据
```

```bash
.venv/bin/python -m unittest discover -s tests -v
cd web
npm ci
npm test
npm run build
```

从源码构建建议Node22.22.2+；本次在应用服务Node22.22.2干净环境完成构建与406项前端验证，前端单测亦在Node20.19.6环境通过。

完整环境、配置项、测试命令与常见问题见 [部署说明](docs/submission/01-部署与访问.md)。`requirements.txt`是兼容范围，`requirements-submission.lock.txt`是本次应用服务实测精确版本；GPU可选依赖不进入常规平台环境。

## 报告与边界

公开交付准备与账号分离说明见 [PUBLICATION.md](PUBLICATION.md)。源码许可、第三方依赖和数据使用范围见对应说明。

- [技术报告](docs/submission/02-技术报告.md)：问题、路线、架构、算法、模块、实验、效果、不足。
- [数据说明](docs/submission/03-数据说明.md)：字段、预处理、脱敏、授权与复现边界。
- [算法说明](docs/submission/04-模型与算法.md)：模型／检索参数与GPU实验协议。
- [GPU专项报告](docs/submission/08-GPU向量计算测试报告.md)：4090D完整向量编码、近邻计算、显存、吞吐和平台回传验收。
- [测试报告](docs/submission/05-测试报告.md)与[案例展示](docs/submission/06-案例展示.md)：结果均链接机器证据。

数据完善交付版本为同一私有仓库的[v1.0 Release](https://github.com/cccccrj/issuertrace-competition-submission/releases/tag/v1.0)，大型数据和前端产物在Release，源码、9份MD／DOCX／PDF正式报告和原始证据在Git及配套材料包中。第三方数据按相应授权交付；评委需获得私有仓库授权或有效的受限材料访问方式。4090D已完成冻结93209段落／3922查询的3次完整GPU复测，GPU计时与业务接口压测、外部模型调用分别报告。

演示视频约3分14秒及30页答辩PPT已纳入V1.0 Release附件，见[演示材料与文件校验](docs/submission/演示材料.md)。唯一正式提交版本为v1.0，仓库仅保留main分支和v1.0标签；源码、报告与处理后数据运行包按标签及SHA256统一对应。材料入口见[交付索引](docs/submission/00-交付索引.md)。

已验证容量：25用户P95=259.02ms，满足预设2秒交互目标；50独立用户持续30分钟完成37479次请求、零异常，P95=3.16秒（2秒目标的1.58倍），形成扩展容量的可靠性与性能基线。各阶段实数、目标及适用规模详见测试报告。
