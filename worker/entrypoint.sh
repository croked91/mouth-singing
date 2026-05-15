#!/usr/bin/env bash
set -euo pipefail

echo "=== Karaoke Worker ==="
echo "WORKER_MODE=${WORKER_MODE:-gpu}"

# Auto-discover WORKER_ID from the Docker container name unless an explicit
# WORKER_ID env is provided. The container name (e.g.
# `mouth-singing-worker-1`) is stable per replica across `compose down/up`,
# so reset_stale_running_jobs can recover crashed jobs after restarts even
# when running multiple replicas via deploy.replicas.
if [ -z "${WORKER_ID:-}" ]; then
    # Containerd-based runtimes (recent docker on Ubuntu, k3s, etc.) leave
    # /proc/self/cgroup empty and don't expose the container ID via
    # mountinfo, but `hostname` always equals the short container ID, and
    # `docker inspect <short_id>` happily resolves it. The result is the
    # container *name* (e.g. mouth-singing-worker-1) — stable per replica
    # across `compose down/up`.
    if [ -S /var/run/docker.sock ] && command -v docker >/dev/null 2>&1; then
        name=$(docker inspect --format '{{.Name}}' "$(hostname)" 2>/dev/null | sed 's|^/||' || true)
        if [ -n "$name" ]; then
            export WORKER_ID="$name"
        fi
    fi
    if [ -z "${WORKER_ID:-}" ]; then
        # Fallback: hostname is unique among live replicas but is the
        # short container ID, so it changes on recreation. Crash
        # recovery will be best-effort in this mode.
        export WORKER_ID="$(hostname)"
        echo "[entrypoint] WORKER_ID auto-discovery failed (docker inspect did not resolve); falling back to hostname=$WORKER_ID"
    fi
fi
echo "WORKER_ID=$WORKER_ID"

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
