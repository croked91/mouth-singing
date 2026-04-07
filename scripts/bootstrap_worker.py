"""Multi-GPU bootstrap worker for bulk catalog processing.

Architecture: 1 process per GPU, N concurrent async tasks per process.
This avoids CUDA context conflicts that cause SIGSEGV with multiple
processes on the same GPU.

Each GPU process:
  - Has a single CUDA context (one process = one context)
  - Runs N async tasks that share the thread pool
  - Each task has its own pipeline instance (own UVR/CTC/etc.)
  - UVR calls go through asyncio.to_thread → thread pool
  - While one task waits for UVR (GPU), others do CTC (CPU)

Usage (inside Docker with all GPUs):
    python3 -m scripts.bootstrap_worker \
        --db /data/sqlite/karaoke.db \
        --media-root /data/media \
        --model-cache-dir /data/models \
        --qdrant-host qdrant \
        --gpu-ids 0,1 \
        --workers-per-gpu 5
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
        default=4,
        help="Concurrent async tasks per GPU (4 optimal for multi-GPU)",
    )
    p.add_argument("--normalization-stats", default="", help="Feature norm JSON path")
    p.add_argument(
        "--busy-timeout",
        type=int,
        default=30000,
        help="SQLite busy timeout in ms (default: 30000)",
    )
    return p.parse_args()


def main() -> None:
    multiprocessing.set_start_method("spawn")
    args = parse_args()
    gpu_ids = [int(x) for x in args.gpu_ids.split(",")]
    total_workers = len(gpu_ids) * args.workers_per_gpu
    omp_threads = max(2, (os.cpu_count() or 4) // total_workers)

    stop_event = multiprocessing.Event()

    # --- Phase 1: Warmup models (single process) ---
    print("Phase 1: Warming up models...")
    _warmup_models(args)
    print("  Models ready.\n")

    # --- Phase 2: QDrant collections ---
    print("Phase 2: Verifying QDrant collections...")
    _ensure_collections(args)
    print()

    # --- Phase 3: Reset stale jobs ---
    _reset_all_stale(args.db)

    # --- Phase 4: 1 process per GPU, N tasks inside ---
    procs: list[multiprocessing.Process] = []
    for gpu_id in gpu_ids:
        p = multiprocessing.Process(
            target=_gpu_process_entry,
            args=(gpu_id, args.workers_per_gpu, omp_threads, stop_event, args),
            name=f"gpu-{gpu_id}",
        )
        p.start()
        procs.append(p)

    print(
        f"Phase 4: Started {len(gpu_ids)} GPU processes "
        f"({args.workers_per_gpu} tasks each = {total_workers} total) "
        f"on GPUs {gpu_ids}\n"
    )

    def _handle_signal(signum, frame):
        print("\n>>> Shutting down gracefully...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    _monitor_loop(procs, stop_event, args.db)
    print("\nBootstrap complete.")


# ---------------------------------------------------------------------------
# Init helpers
# ---------------------------------------------------------------------------


def _warmup_models(args: argparse.Namespace) -> None:
    """Download/cache all models once before spawning."""
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"

    from karaoke_shared.ml.feature_extractor import FeatureExtractor
    from karaoke_shared.ml.lyric_embedder import LyricEmbedder
    from worker.common.ctc_aligner import CTCAligner

    from worker.gpu.uvr_separator import UVRSeparator

    uvr = UVRSeparator(
        model_cache_dir=args.model_cache_dir, media_root=args.media_root,
    )
    uvr.cleanup()
    del uvr
    print("  BS-RoFormer model cached")

    CTCAligner(batch_size=16)
    print("  CTC aligner cached")

    fe_kw: dict = {}
    if args.normalization_stats:
        fe_kw["normalization_stats_path"] = args.normalization_stats
    FeatureExtractor(**fe_kw)
    print("  Feature extractor ready")

    LyricEmbedder(cache_dir=args.model_cache_dir)
    print("  Lyric embedder cached")


def _ensure_collections(args: argparse.Namespace) -> None:
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
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        """UPDATE job_queue SET status='pending', locked_by=NULL, locked_at=NULL
           WHERE status='running' AND locked_by LIKE 'bootstrap-%'"""
    ).rowcount
    conn.commit()
    conn.close()
    if count:
        print(f"Phase 3: Reset {count} stale bootstrap jobs\n")
    else:
        print("Phase 3: No stale jobs\n")


# ---------------------------------------------------------------------------
# GPU process: 1 per GPU, runs N async tasks
# ---------------------------------------------------------------------------


def _gpu_process_entry(
    gpu_id: int,
    num_tasks: int,
    omp_threads: int,
    stop_event: multiprocessing.Event,
    args: argparse.Namespace,
) -> None:
    """Entry point for a GPU process. Runs N concurrent async tasks."""
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    os.environ["OMP_NUM_THREADS"] = str(omp_threads)

    import asyncio

    asyncio.run(_gpu_async_main(gpu_id, num_tasks, stop_event, args))


async def _gpu_async_main(
    gpu_id: int,
    num_tasks: int,
    stop_event: multiprocessing.Event,
    args: argparse.Namespace,
) -> None:
    """Create N pipeline instances and run them as concurrent tasks."""
    import asyncio

    import aiosqlite
    from qdrant_client import QdrantClient

    from karaoke_shared.ml.feature_extractor import FeatureExtractor
    from karaoke_shared.ml.lyric_embedder import LyricEmbedder
    from karaoke_shared.repositories.qdrant_repository import QDrantRepository
    from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
    from karaoke_shared.services.job_service import JobService
    from worker.bootstrap.pipeline import BootstrapPipeline
    from worker.common.ctc_aligner import CTCAligner

    # Shared DB connection (single process)
    conn = await aiosqlite.connect(args.db)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute(f"PRAGMA busy_timeout={args.busy_timeout}")

    repo = SQLiteRepository(conn)
    job_service = JobService(repo)

    # Shared QDrant client
    qc = QdrantClient(host=args.qdrant_host, port=args.qdrant_port)
    qr = QDrantRepository(qc)

    # Shared feature extractor + lyric embedder (read-only, thread-safe)
    fe_kw: dict = {}
    if args.normalization_stats:
        fe_kw["normalization_stats_path"] = args.normalization_stats
    feature_extractor = FeatureExtractor(**fe_kw)
    lyric_embedder = LyricEmbedder(cache_dir=args.model_cache_dir)

    # Create N pipelines, each with its own UVR separator + CTC
    from worker.gpu.uvr_separator import UVRSeparator

    pipelines: list[BootstrapPipeline] = []
    for i in range(num_tasks):
        sep = UVRSeparator(
            model_cache_dir=args.model_cache_dir,
            media_root=args.media_root,
        )
        ctc = CTCAligner(batch_size=16)

        pipeline = BootstrapPipeline(
            job_service=job_service,
            uvr=sep,
            repo=repo,
            ctc_aligner=ctc,
            feature_extractor=feature_extractor,
            lyric_embedder=lyric_embedder,
            qdrant_repo=qr,
        )
        pipelines.append(pipeline)

    print(f"[gpu-{gpu_id}] {num_tasks} pipelines initialized")

    # Launch N concurrent task loops
    tasks = []
    for i in range(num_tasks):
        worker_id = f"bootstrap-gpu{gpu_id}-w{i}"
        task = asyncio.create_task(
            _task_loop(worker_id, pipelines[i], job_service, repo, stop_event)
        )
        tasks.append(task)

    # Wait for all tasks to complete
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Cleanup
    for p in pipelines:
        p.cleanup()
    await conn.close()

    total = sum(r for r in results if isinstance(r, int))
    print(f"[gpu-{gpu_id}] All tasks done. Total processed: {total}")


async def _task_loop(
    worker_id: str,
    pipeline,
    job_service,
    repo,
    stop_event: multiprocessing.Event,
) -> int:
    """Single async task loop — polls and processes jobs."""
    import asyncio

    processed = 0
    try:
        while not stop_event.is_set():
            job = await job_service.poll_and_lock(worker_id)
            if job is None:
                pending = await repo.poll_pending(limit=1)
                if not pending:
                    print(f"[{worker_id}] No more jobs")
                    break
                await asyncio.sleep(2)
                continue

            await pipeline.process(job)
            processed += 1

            if processed % 10 == 0:
                print(f"[{worker_id}] {processed} done")
    except Exception as exc:
        print(f"[{worker_id}] ERROR: {exc}")

    print(f"[{worker_id}] Finished ({processed} tracks)")
    return processed


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


def _monitor_loop(
    procs: list[multiprocessing.Process],
    stop_event: multiprocessing.Event,
    db_path: str,
) -> None:
    start_time = time.time()

    while not stop_event.is_set():
        time.sleep(30)

        alive = [p for p in procs if p.is_alive()]
        dead = [p for p in procs if not p.is_alive() and p.exitcode not in (None, 0)]
        for p in dead:
            print(f"  WARNING: {p.name} died (exit {p.exitcode})")

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA busy_timeout=5000")
            total = conn.execute("SELECT COUNT(*) FROM job_queue").fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM job_queue WHERE status='completed'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM job_queue WHERE status='failed'"
            ).fetchone()[0]
            running = conn.execute(
                "SELECT COUNT(*) FROM job_queue WHERE status='running'"
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
                f"procs={len(alive)} | "
                f"rate={rate:.0f}/h ETA={eta_h:.1f}h"
            )
        except Exception:
            pass

        if not alive:
            print("[monitor] All GPU processes exited")
            break

    for p in procs:
        p.join(timeout=60)
        if p.is_alive():
            p.kill()


if __name__ == "__main__":
    main()
