"""Audio Worker entry point.

Runs an asyncio event loop that polls for pending jobs every N seconds and
processes them through the AudioPipeline. Supports graceful shutdown via
SIGTERM and SIGINT — the poller finishes the current job (if any) before
exiting.

Usage::

    python -m app.main
"""

from __future__ import annotations

import asyncio
import pathlib
import signal

import aiosqlite
import structlog

from karaoke_shared.services.job_service import JobService

from app.config import settings
from app.pipeline.audio_pipeline import AudioPipeline
from app.pipeline.sonoix_client import SonoixClient
from app.pipeline.uvr_separator import UVRSeparator
from app.pipeline.video_generator import VideoGenerator

logger = structlog.get_logger(__name__)


async def _open_db(db_path: str) -> aiosqlite.Connection:
    """Open an existing SQLite database in WAL mode.

    The worker never creates the schema — that is the backend's responsibility.
    It only reads and writes to the tables the backend has already set up.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        An open aiosqlite connection with Row factory and WAL mode enabled.
    """
    path = pathlib.Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5s on writer contention
    return conn


class JobPoller:
    """Polls for pending jobs and dispatches them to the pipeline.

    Runs a tight loop: check for a job, process it if found, or sleep for
    ``poll_interval`` seconds if the queue is empty.

    Args:
        pipeline: The AudioPipeline to dispatch jobs to.
        job_service: Used to poll and lock jobs.
        worker_id: Unique identifier stamped on locked jobs.
        poll_interval: Seconds to sleep when the queue is empty.
    """

    def __init__(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        worker_id: str,
        poll_interval: float,
    ) -> None:
        self.pipeline = pipeline
        self.job_service = job_service
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self._running = True

    def stop(self) -> None:
        """Signal the poller to exit after the current job completes."""
        logger.info("shutdown_requested", worker_id=self.worker_id)
        self._running = False

    async def run(self) -> None:
        """Main polling loop. Runs until ``stop()`` is called."""
        logger.info("poller_started", worker_id=self.worker_id, interval=self.poll_interval)

        while self._running:
            try:
                job = await self.job_service.poll_and_lock(self.worker_id)

                if job is not None:
                    logger.info("job_acquired", job_id=job.id, track_id=job.track_id)
                    await self.pipeline.process(job)
                    logger.info("job_processed", job_id=job.id)
                else:
                    await asyncio.sleep(self.poll_interval)

            except Exception as exc:
                logger.error("poller_error", error=str(exc))
                await asyncio.sleep(self.poll_interval)


async def main() -> None:
    """Worker entry point. Sets up logging, DB, and starts the polling loop."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

    logger.info("worker_starting", worker_id=settings.worker_id)

    db = await _open_db(settings.database_url)

    try:
        from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

        repo = SQLiteRepository(db)
        job_service = JobService(repo)
        uvr = UVRSeparator(settings.model_cache_dir, settings.media_root)
        sonoix: SonoixClient | None = None
        video_gen: VideoGenerator | None = None

        if settings.sonoix_api_key:
            sonoix = SonoixClient(
                api_key=settings.sonoix_api_key,
                api_url=settings.sonoix_api_url,
                timeout=settings.sonoix_timeout,
            )
            video_gen = VideoGenerator(settings.media_root)
            logger.info("sonoix_enabled", api_url=settings.sonoix_api_url)
        else:
            logger.warning("sonoix_api_key_not_set", hint="transcription and video steps will be skipped")

        pipeline = AudioPipeline(job_service, uvr, repo, sonoix, video_gen)

        poller = JobPoller(
            pipeline=pipeline,
            job_service=job_service,
            worker_id=settings.worker_id,
            poll_interval=settings.poll_interval_sec,
        )

        # Wire up graceful shutdown on SIGTERM (Docker stop) and SIGINT (Ctrl-C).
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, poller.stop)

        await poller.run()

    finally:
        await db.close()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
