#!/usr/bin/env bash
set -euo pipefail

MODEL_DIR="${MODEL_CACHE_DIR:-/data/models}"
MODEL_FILE="$MODEL_DIR/UVR-MDX-NET-Voc_FT.onnx"
MODEL_URL="https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/UVR-MDX-NET-Voc_FT.onnx"

mkdir -p "$MODEL_DIR"

if [ ! -f "$MODEL_FILE" ]; then
    echo "Downloading UVR model (~170 MB)..."
    wget -q --show-progress -O "$MODEL_FILE" "$MODEL_URL"
    echo "Model downloaded."
else
    echo "UVR model found at $MODEL_FILE"
fi

echo "Starting worker..."
exec python -m app.main
