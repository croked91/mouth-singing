"""JobService — thin service layer wrapping SQLiteRepository job methods.

Used by both the backend (to enqueue jobs) and the worker (to poll, lock,
and update job state). Keeps job orchestration logic in one place and out
of the API and worker layers.
"""

from __future__ import annotations

from karaoke_shared.models.job import Job, JobCreate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository


class JobService:
    """Orchestrates job lifecycle operations on top of SQLiteRepository.

    Args:
        repo: An open SQLiteRepository backed by a shared aiosqlite connection.
    """

    def __init__(self, repo: SQLiteRepository) -> None:
        self.repo = repo

    async def create_job(self, track_id: str, priority: int = 1) -> Job:
        """Enqueue a new processing job for the given track.

        Args:
            track_id: The ID of the track to process.
            priority: Scheduling priority; higher values are picked up first.

        Returns:
            The newly created Job record.
        """
        return await self.repo.create_job(JobCreate(track_id=track_id, priority=priority))

    async def poll_and_lock(self, worker_id: str) -> Job | None:
        """Poll for the highest-priority pending job and lock it atomically.

        The two-step poll-then-lock pattern means a second worker that races
        to lock the same job will see ``lock_job`` return ``False`` and get
        ``None`` back here — no double-processing.

        Args:
            worker_id: A unique identifier for the calling worker instance.

        Returns:
            The locked Job if one was available, otherwise ``None``.
        """
        jobs = await self.repo.poll_pending(limit=1)
        if not jobs:
            return None

        job = jobs[0]
        locked = await self.repo.lock_job(job.id, worker_id)
        if not locked:
            # Another worker claimed it between our poll and lock calls.
            return None

        return await self.repo.get_job(job.id)

    async def mark_step(self, job_id: str, step: str, progress: int) -> None:
        """Record the current pipeline step and progress percentage.

        Args:
            job_id: The job to update.
            step: Human-readable step name, e.g. ``'separating'``.
            progress: Completion percentage for this step (0–100).
        """
        await self.repo.mark_step(job_id, step, progress)

    async def mark_completed(self, job_id: str, result: dict) -> None:
        """Mark a job as successfully completed and store its result payload.

        Args:
            job_id: The job to complete.
            result: Arbitrary dict with output paths and metadata.
        """
        await self.repo.complete_job(job_id, result)

    async def mark_failed(self, job_id: str, error: str) -> None:
        """Record a job failure and trigger retry logic.

        The repository increments the attempt counter. If attempts are still
        below ``max_attempts``, the job is reset to 'pending' for the next
        worker poll. Otherwise it is moved to 'failed'.

        Args:
            job_id: The job that failed.
            error: Error message to persist.
        """
        await self.repo.fail_job(job_id, error)

    async def get_job(self, job_id: str) -> Job | None:
        """Return a job by primary key, or ``None`` if not found.

        Args:
            job_id: The job to look up.
        """
        return await self.repo.get_job(job_id)
