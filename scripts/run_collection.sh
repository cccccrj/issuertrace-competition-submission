#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# A lock prevents overlapping hourly/daily jobs from multiplying domain traffic.
exec 9>data/collector.lock
if ! flock -n 9; then
  echo "collector already running; skip"
  exit 0
fi

python3 -m collector.collect "$@"
python3 -m collector.report
