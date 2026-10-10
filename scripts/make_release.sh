#!/usr/bin/env bash
# 发布一个版本：部署包 + 同一提交的源码 bundle + 校验和，放进一个以日期和提交命名的目录。
#
# package_deploy.sh 只管「一个包」；一次发布还要回答另外两个问题——这个包对应仓库里的哪一次提交，
# 以及目标机连不上 GitHub 时源码从哪来。此前这两件是手动补的（2026-09-23 那两次），
# 于是有没有 bundle、校验和算没算，取决于那天记不记得。
#
# 发布前的三道闸都会让脚本直接退出，而不是带着问题打出一个包：
#   1. 工作区必须干净——包里的 COMMIT 要能在仓库里找到一模一样的代码；
#   2. 当前分支必须已推送——否则 COMMIT 指向一个只存在于这台机器上的提交；
#   3. web/dist 不能比 web/src 旧（package_deploy.sh 自己也查，这里提前说清楚怎么修）。
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_ROOT="${1:-/root}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "工作区有未提交的改动——先提交，包里的 COMMIT 才对得上仓库：" >&2
  git status --short >&2
  exit 1
fi
git fetch --quiet origin "$BRANCH" || { echo "取不到 origin/$BRANCH，确认网络与远程配置" >&2; exit 1; }
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$BRANCH")" ]]; then
  echo "$BRANCH 与 origin/$BRANCH 不一致——先 git push，否则 COMMIT 指向一个只在本机存在的提交" >&2
  exit 1
fi
if [[ -n "$(find web/src -newer web/dist/index.html -type f -print -quit 2>/dev/null)" ]]; then
  echo "web/dist 比 web/src 旧——先 cd web && npm run build" >&2
  exit 1
fi

SHA="$(git rev-parse --short HEAD)"
OUT="$OUT_ROOT/lcei-release-$(date -u +%Y%m%d)-$SHA"
if [[ -e "$OUT" ]]; then
  echo "$OUT 已存在——同一提交不重复发布；要重打请先移走它" >&2
  exit 1
fi
mkdir -p "$OUT"

./scripts/package_deploy.sh --with-code --out "$OUT"

echo "==> 源码 bundle（$BRANCH @ $SHA）"
git bundle create "$OUT/code-$SHA.bundle" "$BRANCH"
git bundle verify --quiet "$OUT/code-$SHA.bundle"

echo "==> 校验和"
(cd "$OUT" && sha256sum deploy-*.tar.gz "code-$SHA.bundle" > SHA256SUMS)

echo
echo "发布完成：$OUT"
cat "$OUT/SHA256SUMS"
cat <<NEXT

目标机上：
  sha256sum -c SHA256SUMS                         # 两行都应是 OK
  git clone -b $BRANCH code-$SHA.bundle 项目目录   # 连不上 GitHub 时用它取源码；连得上就直接 git clone
  tar xzf deploy-*.tar.gz --strip-components=1 -C 项目目录
  然后照包内 README.txt 与 docs/submission/01-部署与访问.md 落地
NEXT
