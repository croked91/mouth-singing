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
        """Atomically find the highest-priority pending job and lock it.

        Uses a single UPDATE … RETURNING query in the repository, so no
        other worker can grab the same job between the read and write.

        Args:
            worker_id: A unique identifier for the calling worker instance.

        Returns:
            The locked Job if one was available, otherwise ``None``.
        """
        return await self.repo.poll_and_lock(worker_id)

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

    async def mark_permanently_failed(self, job_id: str, error: str) -> None:
        """Mark a job as failed without retry, regardless of max_attempts.

        Use this for errors where retrying would repeat expensive work
        (e.g. lyrics search failure after UVR separation).
        """
        await self.repo.fail_job_permanently(job_id, error)

    async def get_job(self, job_id: str) -> Job | None:
        """Return a job by primary key, or ``None`` if not found.

        Args:
            job_id: The job to look up.
        """
        return await self.repo.get_job(job_id)
