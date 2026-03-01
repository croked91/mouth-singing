#!/usr/bin/env bash
set -euo pipefail

# Setup a bootstrap worker environment in a conda env.
#
# Prerequisites:
#   - WSL2 with NVIDIA GPU drivers (host Windows driver is sufficient)
#   - Miniforge or Miniconda installed and in PATH
#
# Usage:
#   bash setup-worker.sh              # creates env named "bootstrap"
#   bash setup-worker.sh my-env-name  # custom env name

CONDA_ENV="${1:-bootstrap}"
PYTHON_VERSION="3.12"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/../../.."

echo "=== Creating conda environment: ${CONDA_ENV} ==="
conda create -n "${CONDA_ENV}" python="${PYTHON_VERSION}" -y

# Activate inside the script (works for both conda and mamba).
eval "$(conda shell.bash hook)"
conda activate "${CONDA_ENV}"

echo "=== Installing PyTorch with CUDA 12.8 ==="
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

echo "=== Installing project packages ==="
pip install -e "${PROJECT_ROOT}/shared[ml]"
pip install -e "${PROJECT_ROOT}/worker"
pip install -e "${PROJECT_ROOT}/bootstrap"

echo "=== Installing WhisperX ==="
pip install whisperx

echo "=== Replacing onnxruntime with GPU variant ==="
pip install onnxruntime-gpu

echo "=== Verifying CUDA ==="
python -c "
import torch
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    print(f'VRAM: {torch.cuda.get_device_properties(0).total_mem / 1024**3:.1f} GB')
else:
    print('WARNING: No CUDA GPU detected. Processing will be slow on CPU.')
"

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  Remote mode (WSL2 + SSH to VPS):"
echo "    bash ${SCRIPT_DIR}/run-bootstrap.sh --limit 5"
echo ""
echo "  GPU server mode (disk attached locally):"
echo "    bash ${SCRIPT_DIR}/run-gpu-server.sh /path/to/mp3s /path/to/output"
