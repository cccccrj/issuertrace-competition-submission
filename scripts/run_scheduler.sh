#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Local environment files are ignored by Git and may contain proxy credentials.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

: "${COLLECTOR_CONTACT:?Set COLLECTOR_CONTACT in .env or the service environment}"
exec python3 -m collector.scheduler "$@"
