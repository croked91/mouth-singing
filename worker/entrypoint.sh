#!/usr/bin/env bash
set -euo pipefail

echo "=== Karaoke Worker ==="
echo "WORKER_MODE=${WORKER_MODE:-gpu}"
echo "WORKER_ID=${WORKER_ID:-worker-1}"

# Project root must be on PYTHONPATH so `from worker.xxx` imports resolve
export PYTHONPATH="${PYTHONPATH:-}:/project"

exec python -m worker.app.main
