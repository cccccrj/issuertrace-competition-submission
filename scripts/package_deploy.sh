#!/usr/bin/env bash
# Build one archive holding everything a server needs that Git does not carry.
#
# The platform reads its conclusions out of SQLite and nothing else, so the
# default archive is the database plus the audit log — about 50 MB against the
# 2.9 GB that sits under data/.  The raw evidence is opt-in because it is only
# needed to re-run the processor or to recompute the stored hashes on the
# server; a read-only deployment never opens those files.
#
# Secrets are never packaged.  .env is rebuilt on the target (accounts travel inside the
# database as pbkdf2 hashes; config/accounts.json is no longer read), and the
# archive carries a checklist rather than the values.
set -euo pipefail
cd "$(dirname "$0")/.."

WITH_RAW=0
WITH_CODE=0
OUT_DIR="${TMPDIR:-/tmp}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-raw) WITH_RAW=1; shift ;;
    --with-code) WITH_CODE=1; shift ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    -h|--help)
      awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
      echo
      echo "用法: scripts/package_deploy.sh [--with-raw] [--with-code] [--out 目录]"
      echo "  --with-raw   额外打包 data/raw 与 data/manual_inbox（原始证据，约 1GB），"
      echo "               仅在服务器需要重跑处理器或复算哈希时使用"
      echo "  --with-code  把当前提交的源码也打进去，服务器无法 git clone 时用"
      exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

DB=data/state/collector.sqlite3
[[ -f "$DB" ]] || { echo "找不到 $DB" >&2; exit 1; }
# The client bundle travels in the archive rather than being built on the target.
# Without it api/main.py has nothing to mount and every page is a 404, so a
# package missing it is a broken package -- fail here, not on the server.
[[ -f web/dist/index.html ]] || {
  echo "找不到 web/dist/index.html —— 先构建前端：cd web && npm install && npm run build" >&2
  exit 1
}
# 陈旧的 dist 比缺失的 dist 更坏：页面会打开，只是跑的是上一版代码。路由拆包之后尤其如此
# ——index.html 引的是带哈希的块名，源码改了而没重新构建，装出来的就是旧界面。
if [[ -n "$(find web/src -newer web/dist/index.html -type f -print -quit 2>/dev/null)" ]]; then
  echo "web/src 比 web/dist 新 —— 先重新构建前端：cd web && npm run build" >&2
  exit 1
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PAYLOAD="$STAGE/deploy-$STAMP"
mkdir -p "$PAYLOAD/data/state" "$PAYLOAD/data/logs"

echo "==> 一致性快照（sqlite backup API，不是 cp）"
# cp on a live database can copy a torn page set when a WAL is in flight; the
# backup API takes a consistent snapshot while the writer keeps working.
python3 - "$DB" "$PAYLOAD/data/state/collector.sqlite3" <<'PY'
import sqlite3, sys
source, target = sys.argv[1], sys.argv[2]
src = sqlite3.connect(source)
dst = sqlite3.connect(target)
with dst:
    src.backup(dst)
dst.close()
src.close()
PY

# 评测库是另一个文件，「模型与规则质量」页读它。此前不在包里，目标机上那一页只会说
# 「评测数据未随本次部署分发」——2026-09-08 那次发布是在包外手动附上的。
if [[ -f data/state/benchmark.sqlite3 ]]; then
  python3 - data/state/benchmark.sqlite3 "$PAYLOAD/data/state/benchmark.sqlite3" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
fi

echo "==> 部署文档与配置模板"
mkdir -p "$PAYLOAD/docs/submission" "$PAYLOAD/config" "$PAYLOAD/deploy"
# The archive has to stand on its own: whoever unpacks it on a bare server
# should not have to go back to the repository to find out what to do next.
cp docs/submission/01-部署与访问.md "$PAYLOAD/docs/submission/"
cp docs/submission/08-GPU向量计算测试报告.md "$PAYLOAD/docs/submission/"
cp .env.example "$PAYLOAD/"
for example in deploy/*.example; do
  [[ -e "$example" ]] && cp "$example" "$PAYLOAD/deploy/"
done

echo "==> 前端构建产物 web/dist"
# Shipping the built bundle is what makes the archive self-sufficient: the
# target then needs no Node, no npm registry and no network at all.  It is also
# the cheapest rollback this project has -- restoring a previous UI is
# restoring one directory.
mkdir -p "$PAYLOAD/web"
cp -a web/dist "$PAYLOAD/web/dist"

if [[ "$WITH_CODE" == "1" ]]; then
  echo "==> 源码（--with-code，当前提交 $(git rev-parse --short HEAD)）"
  git archive --format=tar HEAD | tar x -C "$PAYLOAD"
  git rev-parse HEAD > "$PAYLOAD/COMMIT"
fi

# Dense neighbours, when they have been imported: a few MB, read only by the
# pipeline's fallback adjudication (claim_verdict --retrieval fallback), never by
# the platform.  They are keyed by paragraph id, so they travel with the very
# database snapshot taken above or not at all.
if [[ -f data/state/dense/manifest.json ]]; then
  echo "==> 向量近邻 data/state/dense（已导入时）"
  mkdir -p "$PAYLOAD/data/state"
  cp -a data/state/dense "$PAYLOAD/data/state/dense"
fi

if [[ -f data/logs/collection.jsonl ]]; then
  echo "==> 审计日志"
  cp data/logs/collection.jsonl "$PAYLOAD/data/logs/collection.jsonl"
fi

if [[ "$WITH_RAW" == "1" ]]; then
  echo "==> 原始证据（--with-raw）"
  for dir in data/raw data/manual_inbox data/processed/text; do
    [[ -d "$dir" ]] || continue
    mkdir -p "$PAYLOAD/$(dirname "$dir")"
    cp -a "$dir" "$PAYLOAD/$dir"
  done
fi

cat > "$PAYLOAD/README.txt" <<'NOTE'
部署包内容
==========
data/state/collector.sqlite3   平台读取的主数据文件（处理层结论 + 平台自己写的处置、留痕、账号）
data/state/benchmark.sqlite3   评测库，「模型与规则质量」页读它；缺失时只有那一页为空
web/dist/                      前端构建产物，api/main.py 挂载的就是它；包内已含，目标机无需 Node
data/logs/collection.jsonl     审计页读取（缺失时该页为空，其余页面正常）
data/state/dense/               BGE-M3 向量近邻（导入过才有）；只有处理层的兜底判定（claim_verdict --retrieval fallback）读它，平台不读，缺了不影响任何页面
docs/submission/08-GPU向量计算测试报告.md  在 GPU 服务器上计算近邻、回平台机评测与导入的步骤
docs/submission/01-部署与访问.md          完整起停手册：环境、账号、本机运行、公网部署、自检、依赖顺序
.env.example                   环境变量模板
deploy/*.example               systemd 与 cron 模板（安装前必须替换路径）
MANIFEST.json                  每个文件的 SHA-256 与字节数
data/raw, data/manual_inbox,
data/processed/text            仅 --with-raw 时存在；重跑处理器或复算哈希才需要
源码与 COMMIT                  仅 --with-code 时存在；否则用 git clone 取

包里没有、必须在服务器上重建的一样
=================================
.env
   printf 'AUTH_SECRET=%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" >> .env
   另需 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL / COLLECTOR_CONTACT
   研判助手（2026-10-04 起）在请求里现场调模型，所以平台本身也要 LLM_API_KEY / LLM_BASE_URL，
   并建议 ASSISTANT_MODEL=kimi-k3（只影响助手；处理层仍用 LLM_MODEL）。服务器要能出网到网关。
   不配这些平台照样能起，只是助手显示「服务不可用」。
   不配 AUTH_SECRET 的话每次重启所有人掉线。

账号不必重建：它们住在库里的 platform_users 表，随 collector.sqlite3 一起到达（口令是
pbkdf2-sha256 哈希，不是明文）。**这也意味着把库拷到别的环境等于把账号一起拷过去**——
给测试机或演示机用时，落地后先 disable 掉不该在那台机器上存在的账号：
   PYTHONPATH=. python3 -m api.users list
   PYTHONPATH=. python3 -m api.users disable --username <谁>
库里一个账号都没有时（例如只传了空库），用命令行建第一个管理员：
   PYTHONPATH=. python3 -m api.users create --username admin --role admin

落地步骤
========
  # 源码：包里没有则先 clone；用了 --with-code 就跳过这步，直接解包即可
  git clone <仓库> && cd <项目>
  tar xzf deploy-*.tar.gz --strip-components=1     # 解到项目根目录
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  python3 - <<'PY'
import hashlib, json, pathlib
manifest = json.loads(pathlib.Path("MANIFEST.json").read_text(encoding="utf-8"))
for name, meta in manifest["files"].items():
    if name == "MANIFEST.json":
        continue
    actual = hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()
    print(("OK  " if actual == meta["sha256"] else "损坏") + " " + name)
PY
  # 补齐 .env。
  #
  # 迁移**只在「库比代码旧」时**才跑——即只换了代码、保留目标机自己的旧库。整套换新（本包自带的
  # 库与源码是同一版）时**跳过**：finalize 会把整条派生链按「今天」重算，「最近 30 天」的窗口一挪，
  # 专题状态与风险事项就跟打包那台机器上的不一样了，这不是一个空操作。
  #   .venv/bin/python -m processor.repair_labels
  #   .venv/bin/python -m processor.finalize
  #   .venv/bin/python -m processor.relabel
  #   .venv/bin/python -m processor.unverifiable     # 无法核验子类；整套换新时库里已经有了
  #
  # 起服务（或 systemctl restart）。**必须是新进程**：旧进程不认识新接口，页面会印出 unknown endpoint。
  # 自检：
  set -a; . ./.env; set +a
  .venv/bin/python -m unittest discover -s tests -t .
  .venv/bin/python -m processor.verify --require-ai-complete --require-verdicts --require-analysis
  # 起服务后确认 /api/health 的 status 是 ok 而不是 schema_outdated，并且带着 companies 列表；
  # 未登录访问 /api/risks/investigations、/api/companies/<公司>、/api/audit/actions、
  # /api/risks/tasks、/api/users/reviewers 应为 401（404 = 旧进程）
  # 未登录 POST /api/assistant/stream 也应为 401；登录后在研判档案页问一句，「查证过程」应逐步出现

verify 返回 ok: true 且 errors: 0，才算库传完整。
完整起停手册见 docs/submission/01-部署与访问.md。
NOTE

echo "==> 清单与校验和"
python3 - "$PAYLOAD" "$WITH_RAW" "$WITH_CODE" <<'PY'
import hashlib, json, pathlib, sys
payload, with_raw, with_code = pathlib.Path(sys.argv[1]), sys.argv[2] == "1", sys.argv[3] == "1"
files = {}
for path in sorted(payload.rglob("*")):
    if path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[str(path.relative_to(payload))] = {"sha256": digest, "bytes": path.stat().st_size}
(payload / "MANIFEST.json").write_text(
    json.dumps({"includes_raw_evidence": with_raw, "includes_source": with_code,
                "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
PY

# --out names a directory the caller wants the archive in, and it may not exist
# yet.  Failing at the tar step wastes the whole build -- the snapshot, the
# hashes and the manifest are all already made by then.
mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/deploy-$STAMP.tar.gz"
echo "==> 打包"
tar czf "$ARCHIVE" -C "$STAGE" "deploy-$STAMP"

echo
echo "完成：$ARCHIVE"
du -h "$ARCHIVE" | awk '{print "  体积：" $1}'
echo "  内容：$(python3 -c "
import json,sys
m=json.load(open('$STAGE/deploy-$STAMP/MANIFEST.json'))
print(len(m['files']), '个文件，原始证据', '已包含' if m['includes_raw_evidence'] else '未包含')")"
echo
echo "下一步："
echo "  scp $ARCHIVE root@<服务器>:/tmp/"
echo "  服务器上解包后照 README.txt 补齐 .env（账号随库到达；web/dist 与评测库已在包内），然后重启服务"
