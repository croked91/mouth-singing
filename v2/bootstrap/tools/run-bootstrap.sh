#!/usr/bin/env bash
set -euo pipefail

# Run the bootstrap pipeline in remote multi-worker mode.
#
# All parameters can be overridden via environment variables.
# Extra arguments are passed through to the CLI (e.g. --limit 5).
#
# Usage:
#   bash run-bootstrap.sh                         # process all remaining tracks
#   bash run-bootstrap.sh --limit 10              # test with 10 tracks
#   bash run-bootstrap.sh --no-delete-remote-source  # dry run

REMOTE_HOST="${BOOTSTRAP_REMOTE_HOST:-root@130.49.170.186}"
WORK_DIR="${BOOTSTRAP_WORK_DIR:-${HOME}/bootstrap_work}"
DEVICE="${BOOTSTRAP_DEVICE:-cuda}"
WHISPER_MODEL="${BOOTSTRAP_WHISPER_MODEL:-medium}"
CONDA_ENV="${BOOTSTRAP_CONDA_ENV:-bootstrap}"

# Remote server paths.
REMOTE_MP3_DIR="${BOOTSTRAP_REMOTE_MP3_DIR:-/root/mp3_library}"
REMOTE_OUTPUT_DIR="${BOOTSTRAP_REMOTE_OUTPUT_DIR:-/var/lib/docker/volumes/v2_media_data/_data}"
REMOTE_DB_PATH="${BOOTSTRAP_REMOTE_DB_PATH:-/var/lib/docker/volumes/v2_sqlite_data/_data/karaoke.db}"

# Derive server IP from the SSH host string (strip "user@" prefix).
SERVER_IP="${REMOTE_HOST#*@}"
LRCLIB_URL="${BOOTSTRAP_LRCLIB_URL:-http://${SERVER_IP}:9876}"
QDRANT_HOST="${BOOTSTRAP_QDRANT_HOST:-${SERVER_IP}}"
QDRANT_PORT="${BOOTSTRAP_QDRANT_PORT:-6333}"

# Activate conda environment.
eval "$(conda shell.bash hook)"
conda activate "${CONDA_ENV}"

# Ensure work directory exists.
mkdir -p "${WORK_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP_DIR="${SCRIPT_DIR}/.."

echo "=== Bootstrap Worker ==="
echo "Remote host:   ${REMOTE_HOST}"
echo "Work dir:      ${WORK_DIR}"
echo "Device:        ${DEVICE}"
echo "Whisper model: ${WHISPER_MODEL}"
echo "Extra args:    $*"
echo ""

cd "${BOOTSTRAP_DIR}"
python -m app.cli \
    "${WORK_DIR}" \
    --output-dir "${WORK_DIR}" \
    --db-path "${WORK_DIR}/karaoke.db" \
    --lrclib-url "${LRCLIB_URL}" \
    --device "${DEVICE}" \
    --whisper-model "${WHISPER_MODEL}" \
    --remote-host "${REMOTE_HOST}" \
    --remote-mp3-dir "${REMOTE_MP3_DIR}" \
    --remote-output-dir "${REMOTE_OUTPUT_DIR}" \
    --remote-db-path "${REMOTE_DB_PATH}" \
    --qdrant-host "${QDRANT_HOST}" \
    --qdrant-port "${QDRANT_PORT}" \
    "$@"
