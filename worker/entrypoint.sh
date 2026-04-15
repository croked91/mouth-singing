#!/usr/bin/env bash
set -euo pipefail

echo "=== Karaoke Worker ==="
echo "WORKER_MODE=${WORKER_MODE:-gpu}"
echo "WORKER_ID=${WORKER_ID:-worker-1}"

# Project root must be on PYTHONPATH so `from worker.xxx` imports resolve
export PYTHONPATH="${PYTHONPATH:-}:/project"

# Link baked-in checkpoints from /opt/models into the volume-mounted
# /data/models so UVRSeparator / BackVocalSeparator can find them.
# The volume mount hides anything we put in /data/models at build time,
# so we symlink at runtime after the volume is attached.
mkdir -p /data/models
for ckpt in /opt/models/*.ckpt; do
    [ -e "$ckpt" ] || continue
    name=$(basename "$ckpt")
    if [ ! -e "/data/models/$name" ]; then
        ln -s "$ckpt" "/data/models/$name"
        echo "linked $name"
    fi
done

exec python -m worker.app.main
