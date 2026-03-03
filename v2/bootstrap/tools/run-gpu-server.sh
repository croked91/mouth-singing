#!/usr/bin/env bash
set -euo pipefail

# Launch bootstrap workers on a multi-GPU server.
#
# The MP3 library must be on a locally-mounted disk (no SSH).
# Each GPU gets its own worker process with atomic file claiming,
# so workers never process the same track.
#
# Safe for preemptible (spot) instances: on restart, stale claims
# are recovered automatically and skip_existing prevents reprocessing.
#
# Usage:
#   bash run-gpu-server.sh /mnt/data/mp3_library
#   bash run-gpu-server.sh /mnt/data/mp3_library /mnt/data/output
#   bash run-gpu-server.sh /mnt/data/mp3_library /mnt/data/output --limit 10
#
# Environment variables (all optional):
#   QDRANT_HOST       QDrant server hostname (default: 130.49.170.186)
#   LRCLIB_URL        lrclib HTTP server URL (default: http://$QDRANT_HOST:9876)
#   WHISPER_MODEL     Whisper model size (default: medium)
#   CONDA_ENV         Conda environment name (default: bootstrap)
#   GPU_IDS           Comma-separated GPU IDs to use (default: auto-detect all)

MP3_DIR="${1:?Usage: $0 /path/to/mp3_library [output_dir] [extra cli args...]}"
OUTPUT_DIR="${2:-${MP3_DIR}/../bootstrap_output}"
shift 2 2>/dev/null || shift 1

QDRANT_HOST="${QDRANT_HOST:-130.49.170.186}"
LRCLIB_URL="${LRCLIB_URL:-http://${QDRANT_HOST}:9876}"
WHISPER_MODEL="${WHISPER_MODEL:-medium}"
CONDA_ENV="${CONDA_ENV:-bootstrap}"
CONTAINER_MEDIA_PREFIX="${CONTAINER_MEDIA_PREFIX:-}"
WORKERS_PER_GPU="${WORKERS_PER_GPU:-3}"
DB_PATH="${OUTPUT_DIR}/karaoke.db"

# Activate conda.
eval "$(conda shell.bash hook)"
conda activate "${CONDA_ENV}"

# Resolve script location → bootstrap package root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP_DIR="${SCRIPT_DIR}/.."

# Ensure output directory exists.
mkdir -p "${OUTPUT_DIR}"

# ---------------------------------------------------------------
# 1. Recover stale claims from previous (possibly killed) run
# ---------------------------------------------------------------
PROCESSING_DIR="${MP3_DIR}/.processing"
mkdir -p "${PROCESSING_DIR}"
STALE_COUNT=$(find "${PROCESSING_DIR}" -maxdepth 1 -name '*.mp3' | wc -l)
if [ "${STALE_COUNT}" -gt 0 ]; then
    mv "${PROCESSING_DIR}"/*.mp3 "${MP3_DIR}/"
    echo "Recovered ${STALE_COUNT} stale claim(s)"
fi

# ---------------------------------------------------------------
# 2. Detect GPUs
# ---------------------------------------------------------------
if [ -n "${GPU_IDS:-}" ]; then
    IFS=',' read -ra GPU_LIST <<< "${GPU_IDS}"
else
    GPU_COUNT=$(nvidia-smi -L 2>/dev/null | wc -l)
    if [ "${GPU_COUNT}" -eq 0 ]; then
        echo "ERROR: No GPUs detected. Use --device cpu in manual mode." >&2
        exit 1
    fi
    GPU_LIST=()
    for i in $(seq 0 $((GPU_COUNT - 1))); do
        GPU_LIST+=("$i")
    done
fi

echo "=== GPU Server Bootstrap ==="
echo "MP3 dir:       ${MP3_DIR}"
echo "Output dir:    ${OUTPUT_DIR}"
echo "DB path:       ${DB_PATH}"
echo "QDrant host:   ${QDRANT_HOST}"
echo "lrclib URL:    ${LRCLIB_URL}"
echo "GPUs:          ${GPU_LIST[*]}"
echo "Workers/GPU:   ${WORKERS_PER_GPU}"
echo "Extra args:    $*"
echo ""

# ---------------------------------------------------------------
# 3. Launch one worker per GPU
# ---------------------------------------------------------------
cd "${BOOTSTRAP_DIR}"

PIDS=()
WORKER_DELAY="${WORKER_DELAY:-5}"
for gpu_id in "${GPU_LIST[@]}"; do
    for w in $(seq 1 "${WORKERS_PER_GPU}"); do
        CUDA_VISIBLE_DEVICES="${gpu_id}" python -m app.cli \
            "${MP3_DIR}" \
            --output-dir "${OUTPUT_DIR}" \
            --db-path "${DB_PATH}" \
            --lrclib-url "${LRCLIB_URL}" \
            --device cuda \
            --whisper-model "${WHISPER_MODEL}" \
            --gpu-id "${gpu_id}" \
            --qdrant-host "${QDRANT_HOST}" \
            ${CONTAINER_MEDIA_PREFIX:+--container-media-prefix "${CONTAINER_MEDIA_PREFIX}"} \
            "$@" &
        PIDS+=($!)
        echo "Worker GPU ${gpu_id}/${w} started (PID $!)"
        sleep "${WORKER_DELAY}"
    done
done

echo ""
TOTAL_WORKERS=$((${#GPU_LIST[@]} * WORKERS_PER_GPU))
echo ""
echo "All ${TOTAL_WORKERS} worker(s) running (${#GPU_LIST[@]} GPUs × ${WORKERS_PER_GPU}). Waiting..."

# ---------------------------------------------------------------
# 4. Wait for all workers, report exit codes
# ---------------------------------------------------------------
EXIT_CODE=0
for pid in "${PIDS[@]}"; do
    if ! wait "${pid}"; then
        echo "Worker PID ${pid} exited with error" >&2
        EXIT_CODE=1
    fi
done

if [ "${EXIT_CODE}" -eq 0 ]; then
    echo "All workers finished successfully"
else
    echo "Some workers failed — check logs above" >&2
fi

exit "${EXIT_CODE}"
