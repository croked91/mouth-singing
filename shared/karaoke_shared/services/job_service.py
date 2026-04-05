"""JobService — thin service layer wrapping PgRepository job methods.

Used by both the backend (to enqueue jobs) and the worker (to poll, lock,
and update job state). Optionally publishes progress events via RabbitMQ.
"""

from __future__ import annotations

import structlog

from karaoke_shared.models.job import Job, JobCreate
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.services.progress_publisher import ProgressPublisher

logger = structlog.get_logger(__name__)


class JobService:
    """Orchestrates job lifecycle operations on top of PgRepository.

    Args:
        repo: A PgRepository backed by an asyncpg connection pool.
        publisher: Optional ProgressPublisher for RabbitMQ events.
    """

    def __init__(
        self,
        repo: PgRepository,
        publisher: ProgressPublisher | None = None,
    ) -> None:
        self.repo = repo
        self._publisher = publisher

    async def create_job(self, data: JobCreate) -> Job:
        """Enqueue a new processing job."""
        return await self.repo.create_job(data)

    async def poll_and_lock(self, worker_id: str) -> Job | None:
        """Atomically find the highest-priority pending job and lock it."""
        return await self.repo.poll_and_lock(worker_id)

    async def mark_step(self, job_id: str, step: str, progress: int) -> None:
        """Record the current pipeline step and progress percentage."""
        await self.repo.mark_step(job_id, step, progress)
        if self._publisher:
            try:
                await self._publisher.publish_progress(job_id, step, progress)
            except Exception as exc:
                logger.warning("progress_publish_failed", job_id=job_id, error=str(exc))

    async def mark_completed(self, job_id: str, result: dict) -> None:
        """Mark a job as successfully completed and store its result payload."""
        await self.repo.complete_job(job_id, result)
        if self._publisher:
            track_id = result.get("track_id", "")
            try:
                await self._publisher.publish_completed(job_id, track_id)
            except Exception as exc:
                logger.warning("completed_publish_failed", job_id=job_id, error=str(exc))

    async def mark_failed(self, job_id: str, error: str) -> None:
        """Record a job failure and trigger retry logic."""
        await self.repo.fail_job(job_id, error)
        if self._publisher:
            try:
                await self._publisher.publish_error(job_id, error)
            except Exception as exc:
                logger.warning("error_publish_failed", job_id=job_id, error=str(exc))

    async def mark_permanently_failed(self, job_id: str, error: str) -> None:
        """Mark a job as failed without retry."""
        await self.repo.fail_job_permanently(job_id, error)
        if self._publisher:
            try:
                await self._publisher.publish_error(job_id, error)
            except Exception as exc:
                logger.warning("error_publish_failed", job_id=job_id, error=str(exc))

    async def get_job(self, job_id: str) -> Job | None:
        """Return a job by primary key, or ``None`` if not found."""
        return await self.repo.get_job(job_id)
