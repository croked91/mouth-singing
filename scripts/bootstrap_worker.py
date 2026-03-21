"""Multi-GPU bootstrap worker for bulk catalog processing.

Spawns N worker processes across M GPUs, each running a simplified
pipeline (no Whisper, no LLM, no VAD).

Usage (inside Docker with all GPUs):
    python3 -m scripts.bootstrap_worker \
        --db /data/sqlite/karaoke.db \
        --media-root /data/media \
        --model-cache-dir /data/models \
        --qdrant-host qdrant \
        --gpu-ids 0,1 \
        --workers-per-gpu 10
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import signal
import sqlite3
import time


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bootstrap worker (multi-GPU)")
    p.add_argument("--db", required=True, help="SQLite database path")
    p.add_argument("--media-root", required=True, help="Media root directory")
    p.add_argument("--model-cache-dir", required=True, help="ML model cache dir")
    p.add_argument("--qdrant-host", default="qdrant", help="QDrant hostname")
    p.add_argument("--qdrant-port", type=int, default=6333)
    p.add_argument(
        "--gpu-ids",
        default="0",
        help="Comma-separated GPU IDs, e.g. '0,1,2,3'",
    )
    p.add_argument(
        "--workers-per-gpu",
        type=int,
        default=3,
        help="Workers per GPU (RTX4090: 3-4, A100-40: 5-6, A100-80: 10-12)",
    )
    p.add_argument("--normalization-stats", default="", help="Feature norm JSON path")
    p.add_argument(
        "--busy-timeout",
        type=int,
        default=10000,
        help="SQLite busy timeout in ms (default: 10000)",
    )
    return p.parse_args()


def main() -> None:
    multiprocessing.set_start_method("spawn")  # Required for CUDA safety
    args = parse_args()
    gpu_ids = [int(x) for x in args.gpu_ids.split(",")]
    total_workers = len(gpu_ids) * args.workers_per_gpu
    omp_threads = max(2, (os.cpu_count() or 4) // total_workers)

    stop_event = multiprocessing.Event()

    # --- Phase 1: Download models in single process (avoid race) ---
    print("Phase 1: Warming up models (single process)...")
    _warmup_models(args)
    print("  Models ready.\n")

    # --- Phase 2: Ensure QDrant collections exist ---
    print("Phase 2: Verifying QDrant collections...")
    _ensure_collections(args)
    print()

    # --- Phase 3: Reset stale bootstrap jobs from previous runs ---
    _reset_all_stale(args.db)

    # --- Phase 4: Spawn worker processes ---
    procs: list[multiprocessing.Process] = []
    for gpu_id in gpu_ids:
        for w_idx in range(args.workers_per_gpu):
            worker_id = f"bootstrap-gpu{gpu_id}-w{w_idx}"
            p = multiprocessing.Process(
                target=_worker_entry,
                args=(gpu_id, worker_id, omp_threads, stop_event, args),
                name=worker_id,
            )
            p.start()
            procs.append(p)

    print(
        f"Phase 4: Started {total_workers} workers on GPUs {gpu_ids} "
        f"(OMP_NUM_THREADS={omp_threads})\n"
    )

    # --- Signal handling for graceful shutdown ---
    def _handle_signal(signum, frame):
        print("\n>>> Received signal, shutting down gracefully...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # --- Phase 5: Monitor loop ---
    _monitor_loop(procs, stop_event, args.db)
    print("\nBootstrap complete.")


# ---------------------------------------------------------------------------
# Initialization helpers (run in main process before spawning workers)
# ---------------------------------------------------------------------------


def _warmup_models(args: argparse.Namespace) -> None:
    """Download/cache all ML models once to avoid download races."""
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"

    from karaoke_shared.ml.feature_extractor import FeatureExtractor
    from karaoke_shared.ml.lyric_embedder import LyricEmbedder
    from worker.common.ctc_aligner import CTCAligner
    from worker.gpu.uvr_separator import UVRSeparator

    # UVR / BS-Roformer
    uvr = UVRSeparator(
        model_cache_dir=args.model_cache_dir,
        media_root=args.media_root,
    )
    uvr.cleanup()
    del uvr
    print("  UVR model cached")

    # CTC aligner (ONNX model)
    CTCAligner(batch_size=16)
    print("  CTC aligner model cached")

    # Feature extractor (no model download, but validates stats file)
    fe_kw: dict = {}
    if args.normalization_stats:
        fe_kw["normalization_stats_path"] = args.normalization_stats
    FeatureExtractor(**fe_kw)
    print("  Feature extractor ready")

    # Lyric embedder (sentence-transformers download)
    LyricEmbedder(cache_dir=args.model_cache_dir)
    print("  Lyric embedder model cached")


def _ensure_collections(args: argparse.Namespace) -> None:
    """Create QDrant collections if they don't exist."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(host=args.qdrant_host, port=args.qdrant_port)
    existing = {c.name for c in client.get_collections().collections}

    for name, dim in [("audio_features", 45), ("lyrics_embeddings", 384)]:
        if name not in existing:
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            print(f"  Created: {name} ({dim}d)")
        else:
            print(f"  Exists:  {name}")


def _reset_all_stale(db_path: str) -> None:
    """Reset ALL running jobs locked by any bootstrap worker."""
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        """UPDATE job_queue
           SET status = 'pending', locked_by = NULL, locked_at = NULL
           WHERE status = 'running' AND locked_by LIKE 'bootstrap-%'"""
    ).rowcount
    conn.commit()
    conn.close()
    if count:
        print(f"Phase 3: Reset {count} stale bootstrap jobs\n")
    else:
        print("Phase 3: No stale jobs\n")


# ---------------------------------------------------------------------------
# Worker subprocess
# ---------------------------------------------------------------------------


def _worker_entry(
    gpu_id: int,
    worker_id: str,
    omp_threads: int,
    stop_event: multiprocessing.Event,
    args: argparse.Namespace,
) -> None:
    """Entry point for each worker subprocess.

    Sets CUDA device and OMP threads, then runs the async worker loop.
    """
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    os.environ["OMP_NUM_THREADS"] = str(omp_threads)

    import asyncio

    asyncio.run(_async_worker(worker_id, stop_event, args))


async def _async_worker(
    worker_id: str,
    stop_event: multiprocessing.Event,
    args: argparse.Namespace,
) -> None:
    """Async worker loop — polls job queue, processes tracks."""
    import aiosqlite
    from qdrant_client import QdrantClient

    from karaoke_shared.ml.feature_extractor import FeatureExtractor
    from karaoke_shared.ml.lyric_embedder import LyricEmbedder
    from karaoke_shared.repositories.qdrant_repository import QDrantRepository
    from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
    from karaoke_shared.services.job_service import JobService
    from worker.bootstrap.pipeline import BootstrapPipeline
    from worker.common.ctc_aligner import CTCAligner
    from worker.gpu.uvr_separator import UVRSeparator

    # Database connection (WAL mode for concurrent access)
    conn = await aiosqlite.connect(args.db)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute(f"PRAGMA busy_timeout={args.busy_timeout}")

    repo = SQLiteRepository(conn)
    job_service = JobService(repo)

    # ML components
    uvr = UVRSeparator(
        model_cache_dir=args.model_cache_dir,
        media_root=args.media_root,
    )
    ctc = CTCAligner(batch_size=16)

    fe_kw: dict = {}
    if args.normalization_stats:
        fe_kw["normalization_stats_path"] = args.normalization_stats
    feature_extractor = FeatureExtractor(**fe_kw)

    lyric_embedder = LyricEmbedder(cache_dir=args.model_cache_dir)

    qdrant_client = QdrantClient(host=args.qdrant_host, port=args.qdrant_port)
    qdrant_repo = QDrantRepository(qdrant_client)

    # Pipeline
    pipeline = BootstrapPipeline(
        job_service=job_service,
        uvr=uvr,
        repo=repo,
        ctc_aligner=ctc,
        feature_extractor=feature_extractor,
        lyric_embedder=lyric_embedder,
        qdrant_repo=qdrant_repo,
    )

    print(f"[{worker_id}] Initialized, starting poll loop")

    processed = 0
    try:
        while not stop_event.is_set():
            job = await job_service.poll_and_lock(worker_id)
            if job is None:
                # Check if there are any pending jobs left at all
                pending = await repo.poll_pending(limit=1)
                if not pending:
                    print(f"[{worker_id}] No more pending jobs, exiting")
                    break
                await asyncio.sleep(2)
                continue

            await pipeline.process(job)
            processed += 1

            if processed % 10 == 0:
                print(f"[{worker_id}] Processed {processed} tracks")

    finally:
        pipeline.cleanup()
        await conn.close()
        print(f"[{worker_id}] Finished. Total processed: {processed}")


# ---------------------------------------------------------------------------
# Monitor loop (main process)
# ---------------------------------------------------------------------------


def _monitor_loop(
    procs: list[multiprocessing.Process],
    stop_event: multiprocessing.Event,
    db_path: str,
) -> None:
    """Monitor worker processes and report progress every 30 seconds."""
    start_time = time.time()

    while not stop_event.is_set():
        time.sleep(30)

        alive = [p for p in procs if p.is_alive()]
        dead = [p for p in procs if not p.is_alive() and p.exitcode not in (None, 0)]

        for p in dead:
            print(f"  WARNING: {p.name} died with exit code {p.exitcode}")

        # Query progress from DB
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA busy_timeout=5000")
            total = conn.execute("SELECT COUNT(*) FROM job_queue").fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM job_queue WHERE status = 'completed'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM job_queue WHERE status = 'failed'"
            ).fetchone()[0]
            running = conn.execute(
                "SELECT COUNT(*) FROM job_queue WHERE status = 'running'"
            ).fetchone()[0]
            conn.close()

            pct = completed / total * 100 if total else 0
            elapsed = time.time() - start_time
            rate = completed / elapsed * 3600 if elapsed > 0 and completed > 0 else 0
            remaining = total - completed - failed
            eta_h = remaining / rate if rate > 0 else 0

            print(
                f"[monitor] {completed}/{total} ({pct:.1f}%) | "
                f"running={running} failed={failed} | "
                f"workers={len(alive)} | "
                f"rate={rate:.0f}/h ETA={eta_h:.1f}h"
            )
        except Exception:
            pass

        if not alive:
            print("[monitor] All workers exited")
            break

    # Wait for all processes to finish
    for p in procs:
        p.join(timeout=60)
        if p.is_alive():
            print(f"  Force-killing {p.name}")
            p.kill()


if __name__ == "__main__":
    main()
