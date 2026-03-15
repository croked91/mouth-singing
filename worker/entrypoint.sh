#!/usr/bin/env bash
set -euo pipefail

echo "Checking models..."
python /worker/tools/download_models.py

echo "Starting worker..."
exec python -m app.main
