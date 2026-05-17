"""Periodic sweeper for orphan pending jobs.

Detects jobs that sit in ``status='pending'`` in PostgreSQL but have no
corresponding message in RabbitMQ — i.e. the RMQ message was lost
(broker volume reset / queue recreated / race between INSERT and
publish / admin purge).

Strategy:
  * Every ``interval_seconds`` walk all pending jobs older than
    ``pending_ttl_seconds`` (using ``updated_at``).
  * For each candidate:
      - if age (from ``created_at``) >= ``hard_fail_ttl_seconds`` →
        give up, ``mark_permanently_failed``;
      - otherwise → re-publish to the ``jobs`` exchange. The worker's
        ``lock_job`` is idempotent: if a duplicate ever surfaces, the
        second consumer sees ``False`` and nacks the duplicate.

Runs as a background ``asyncio.Task`` inside the backend lifespan.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone

import structlog
from karaoke_shared.messaging.rabbitmq import RabbitMQClient
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.services.job_service import JobService

logger = structlog.get_logger(__name__)


class JobSweeper:
    """Background sweeper that recovers orphan pending jobs."""

    def __init__(
        self,
        repo: PgRepository,
        rmq: RabbitMQClient,
        job_service: JobService,
        interval_seconds: int,
        pending_ttl_seconds: int,
        hard_fail_ttl_seconds: int,
    ) -> None:
        self._repo = repo
        self._rmq = rmq
        self._job_service = job_service
        self._interval = interval_seconds
        self._pending_ttl = pending_ttl_seconds
        self._hard_fail_ttl = hard_fail_ttl_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Launch the background sweep loop."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="job_sweeper")
        logger.info(
            "job_sweeper_started",
            interval_sec=self._interval,
            pending_ttl_sec=self._pending_ttl,
            hard_fail_ttl_sec=self._hard_fail_ttl,
        )

    async def stop(self) -> None:
        """Cancel the sweep loop and wait for it to exit."""
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None
        logger.info("job_sweeper_stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let an exception kill the loop — log and keep going.
                logger.exception("job_sweeper_cycle_failed")

    async def _sweep_once(self) -> None:
        candidates = await self._repo.find_stale_pending_jobs(self._pending_ttl)
        if not candidates:
            return

        now = datetime.now(timezone.utc)
        republished = 0
        failed = 0

        for job in candidates:
            try:
                created_at = datetime.fromisoformat(job.created_at)
            except (TypeError, ValueError):
                logger.warning(
                    "job_sweeper_bad_created_at",
                    job_id=job.id,
                    created_at=job.created_at,
                )
                continue

            age_sec = (now - created_at).total_seconds()

            if age_sec >= self._hard_fail_ttl:
                await self._job_service.mark_permanently_failed(
                    job.id,
                    (
                        f"Sweeper hard-fail: pending for {int(age_sec)}s "
                        f">= {self._hard_fail_ttl}s, no progress recorded "
                        "(RMQ message presumed lost)."
                    ),
                )
                failed += 1
                logger.warning(
                    "job_sweeper_hard_failed",
                    job_id=job.id,
                    age_sec=int(age_sec),
                )
                continue

            if not job.mp3_key:
                # Belt & suspenders: find_stale_pending_jobs filters on
                # mp3_key IS NOT NULL, but defend against schema drift.
                logger.warning("job_sweeper_skipped_no_mp3_key", job_id=job.id)
                continue

            body: dict[str, str | int] = {
                "job_id": job.id,
                "mp3_key": job.mp3_key,
            }
            await self._rmq.publish(
                "jobs", "", body, priority=job.priority,
            )
            republished += 1
            logger.info(
                "job_sweeper_republished",
                job_id=job.id,
                age_sec=int(age_sec),
            )

        logger.info(
            "job_sweeper_cycle_done",
            candidates=len(candidates),
            republished=republished,
            hard_failed=failed,
        )
