#!/usr/bin/env bash
set -euo pipefail

export HF_HOME="${MODEL_CACHE_DIR:-/data/models}/huggingface"
mkdir -p "${HF_HOME}"

echo "[entrypoint] Starting worker v3-rc2..."
echo "[entrypoint] HF_HOME=${HF_HOME}"
echo "[entrypoint] MVSEP_API_KEY=${MVSEP_API_KEY:0:8}..."
echo "[entrypoint] OPENAI_API_KEY=${OPENAI_API_KEY:0:8}..."
echo "[entrypoint] GENIUS_TOKEN=${GENIUS_TOKEN:0:8}..."
echo "[entrypoint] LYRIC_EMBEDDER_BACKEND=${LYRIC_EMBEDDER_BACKEND:-local}"

exec python -m app.main
