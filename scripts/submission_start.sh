#!/usr/bin/env bash
# Native CPU deployment of the precomputed analytical snapshot; no GPU needed.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${SUBMISSION_PYTHON:-.venv/bin/python}"
PORT="${SUBMISSION_PORT:-8000}"
HOST="${SUBMISSION_HOST:-127.0.0.1}"
[[ -f data/state/collector.sqlite3 ]] || { echo '先解压同仓库Release 的部署包。'; exit 1; }
[[ -f web/dist/index.html ]] || { echo '先运行 cd web && npm ci && npm run build'; exit 1; }
exec "$PYTHON" -m uvicorn api.main:app --host "$HOST" --port "$PORT" --workers 1
