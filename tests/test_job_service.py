"""Unit tests for JobService.

Covers the full job lifecycle: creation, poll-and-lock (happy path and race
conditions), step marking, completion, and the retry/fail logic inside
mark_failed.

All tests use the ``sqlite_db`` / ``sqlite_repo`` fixtures from conftest.py,
which provide a fresh in-memory aiosqlite connection for every test.
asyncio_mode = "auto" (set in pytest.ini) so no @pytest.mark.asyncio is needed.
"""

from __future__ import annotations

import pytest

from karaoke_shared.models.track import TrackCreate
from karaoke_shared.repositories import SQLiteRepository
from karaoke_shared.services.job_service import JobService


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _track_create(mp3_path: str | None = "/media/song.mp3") -> TrackCreate:
    """Return a minimal TrackCreate with an optional mp3_path."""
    return TrackCreate(
        artist="Test Artist",
        title="Test Title",
        source="catalog",
        mp3_path=mp3_path,
    )


# ---------------------------------------------------------------------------
# Fixture: job_service
# ---------------------------------------------------------------------------


@pytest.fixture
def job_service(sqlite_repo: SQLiteRepository) -> JobService:
    """Return a JobService backed by the in-memory SQLiteRepository."""
    return JobService(sqlite_repo)


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


class TestCreateJob:
    async def test_creates_job_with_pending_status(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """create_job returns a Job with status='pending'."""
        track = await sqlite_repo.create_track(_track_create())

        job = await job_service.create_job(track.id)

        assert job.status == "pending"

    async def test_creates_job_with_correct_track_id(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """create_job stores the supplied track_id on the Job."""
        track = await sqlite_repo.create_track(_track_create())

        job = await job_service.create_job(track.id)

        assert job.track_id == track.id

    async def test_creates_job_with_default_priority(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """create_job defaults to priority=1 when not specified."""
        track = await sqlite_repo.create_track(_track_create())

        job = await job_service.create_job(track.id)

        assert job.priority == 1

    async def test_creates_job_with_custom_priority(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """create_job persists a non-default priority value."""
        track = await sqlite_repo.create_track(_track_create())

        job = await job_service.create_job(track.id, priority=5)

        assert job.priority == 5

    async def test_creates_job_with_zero_attempts(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """create_job initialises attempts to 0."""
        track = await sqlite_repo.create_track(_track_create())

        job = await job_service.create_job(track.id)

        assert job.attempts == 0


# ---------------------------------------------------------------------------
# poll_and_lock
# ---------------------------------------------------------------------------


class TestPollAndLock:
    async def test_returns_job_when_pending_job_exists(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """poll_and_lock returns the pending job and transitions it to 'running'."""
        track = await sqlite_repo.create_track(_track_create())
        created = await job_service.create_job(track.id)

        locked = await job_service.poll_and_lock("worker-1")

        assert locked is not None
        assert locked.id == created.id
        assert locked.status == "running"

    async def test_locked_job_has_worker_id(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """poll_and_lock stamps the worker_id on locked_by."""
        track = await sqlite_repo.create_track(_track_create())
        await job_service.create_job(track.id)

        locked = await job_service.poll_and_lock("worker-abc")

        assert locked is not None
        assert locked.locked_by == "worker-abc"

    async def test_returns_none_when_queue_is_empty(
        self, job_service: JobService
    ) -> None:
        """poll_and_lock returns None when there are no pending jobs."""
        result = await job_service.poll_and_lock("worker-1")

        assert result is None

    async def test_returns_none_when_another_worker_already_locked(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """poll_and_lock returns None when a concurrent worker locked the job first.

        This simulates the TOCTOU race: we poll a pending job, then another
        worker claims it before our lock_job call executes. The service must
        detect this and return None rather than processing the same job twice.
        """
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        # Simulate a competing worker locking the job directly via the repo.
        await sqlite_repo.lock_job(job.id, "worker-other")

        # Now our worker tries to poll-and-lock — it polls the row (now
        # 'running'), so poll_pending returns nothing, and we get None.
        result = await job_service.poll_and_lock("worker-1")

        assert result is None

    async def test_picks_highest_priority_job_first(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """poll_and_lock returns the job with the highest priority."""
        track_low = await sqlite_repo.create_track(_track_create())
        track_high = await sqlite_repo.create_track(_track_create())
        await job_service.create_job(track_low.id, priority=1)
        high_priority_job = await job_service.create_job(track_high.id, priority=10)

        locked = await job_service.poll_and_lock("worker-1")

        assert locked is not None
        assert locked.id == high_priority_job.id


# ---------------------------------------------------------------------------
# mark_step
# ---------------------------------------------------------------------------


class TestMarkStep:
    async def test_updates_current_step(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_step persists the step name to current_step."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await job_service.mark_step(job.id, "separating", 0)

        updated = await job_service.get_job(job.id)
        assert updated is not None
        assert updated.current_step == "separating"

    async def test_updates_progress(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_step persists the progress percentage."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await job_service.mark_step(job.id, "separating", 50)

        updated = await job_service.get_job(job.id)
        assert updated is not None
        assert updated.progress == 50

    async def test_can_update_step_multiple_times(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_step can be called repeatedly and always reflects the latest values."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await job_service.mark_step(job.id, "separating", 0)
        await job_service.mark_step(job.id, "separating", 100)

        updated = await job_service.get_job(job.id)
        assert updated is not None
        assert updated.current_step == "separating"
        assert updated.progress == 100


# ---------------------------------------------------------------------------
# mark_completed
# ---------------------------------------------------------------------------


class TestMarkCompleted:
    async def test_sets_status_to_completed(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_completed transitions the job to status='completed'."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await job_service.mark_completed(job.id, {"output": "path/to/file.wav"})

        updated = await job_service.get_job(job.id)
        assert updated is not None
        assert updated.status == "completed"

    async def test_persists_result_payload(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_completed stores the result dict on the job record."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        result = {"vocals_path": "/media/vocals.wav", "instrumental_path": "/media/inst.wav"}

        await job_service.mark_completed(job.id, result)

        updated = await job_service.get_job(job.id)
        assert updated is not None
        assert updated.result == result


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------


class TestMarkFailed:
    async def test_increments_attempts_on_first_failure(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_failed increments attempts by 1."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        assert job.attempts == 0

        await job_service.mark_failed(job.id, "something broke")

        updated = await job_service.get_job(job.id)
        assert updated is not None
        assert updated.attempts == 1

    async def test_retries_when_under_max_attempts(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_failed resets status to 'pending' if attempts < max_attempts."""
        track = await sqlite_repo.create_track(_track_create())
        # max_attempts defaults to 3; first failure → attempts=1 < 3, retry
        job = await job_service.create_job(track.id)

        await job_service.mark_failed(job.id, "transient error")

        updated = await job_service.get_job(job.id)
        assert updated is not None
        assert updated.status == "pending"

    async def test_fails_permanently_when_max_attempts_exhausted(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_failed sets status='failed' after exhausting all max_attempts.

        We call mark_failed max_attempts times so that attempts reaches
        max_attempts on the final call, triggering permanent failure.
        """
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        max_attempts = job.max_attempts  # default 3

        for i in range(max_attempts):
            # Re-lock before each failure to ensure it can be picked up again
            if i < max_attempts - 1:
                await sqlite_repo.lock_job(job.id, "worker-1")
            await job_service.mark_failed(job.id, f"error #{i + 1}")

        final = await job_service.get_job(job.id)
        assert final is not None
        assert final.status == "failed"

    async def test_persists_error_message(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_failed stores the provided error string on the job."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await job_service.mark_failed(job.id, "UVR crashed with OOM")

        updated = await job_service.get_job(job.id)
        assert updated is not None
        assert updated.error_message == "UVR crashed with OOM"

    async def test_clears_lock_on_failure(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """mark_failed releases the worker lock so the job can be re-acquired."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        # Lock it first, simulating a worker that grabbed it.
        await sqlite_repo.lock_job(job.id, "worker-1")

        await job_service.mark_failed(job.id, "error")

        updated = await job_service.get_job(job.id)
        assert updated is not None
        assert updated.locked_by is None
        assert updated.locked_at is None


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


class TestGetJob:
    async def test_returns_job_by_id(
        self, job_service: JobService, sqlite_repo: SQLiteRepository
    ) -> None:
        """get_job returns the correct job for a given ID."""
        track = await sqlite_repo.create_track(_track_create())
        created = await job_service.create_job(track.id)

        fetched = await job_service.get_job(created.id)

        assert fetched is not None
        assert fetched.id == created.id

    async def test_returns_none_for_unknown_id(
        self, job_service: JobService
    ) -> None:
        """get_job returns None for an ID that does not exist."""
        result = await job_service.get_job("non-existent-id")

        assert result is None
