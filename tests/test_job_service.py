"""Tests for ``karaoke_shared.services.job_service.JobService``.

JobService is a thin orchestrator: it delegates DB writes to PgRepository
and emits progress events through ProgressPublisher when one is supplied.
We mock both collaborators and assert wiring + error tolerance:

  * publisher=None → repo is still called, no exception
  * publisher.* exceptions are swallowed (logged) — DB write must still complete
  * mark_completed picks track_id out of the result dict for the publisher
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from karaoke_shared.services.job_service import JobService


@pytest.fixture
def repo():
    r = MagicMock()
    r.create_job = AsyncMock(return_value="job-stub")
    r.poll_and_lock = AsyncMock(return_value="job-stub")
    r.get_job = AsyncMock(return_value="job-stub")
    r.mark_step = AsyncMock()
    r.complete_job = AsyncMock()
    r.fail_job_permanently = AsyncMock()
    return r


@pytest.fixture
def publisher():
    p = MagicMock()
    p.publish_progress = AsyncMock()
    p.publish_completed = AsyncMock()
    p.publish_error = AsyncMock()
    return p


# ---------------------------------------------------------------------------
# delegation to PgRepository
# ---------------------------------------------------------------------------

async def test_create_job_delegates(repo):
    svc = JobService(repo)
    data = MagicMock()
    result = await svc.create_job(data)
    repo.create_job.assert_awaited_once_with(data)
    assert result == "job-stub"


async def test_poll_and_lock_delegates(repo):
    svc = JobService(repo)
    result = await svc.poll_and_lock("worker-1")
    repo.poll_and_lock.assert_awaited_once_with("worker-1")
    assert result == "job-stub"


async def test_get_job_delegates(repo):
    svc = JobService(repo)
    result = await svc.get_job("j1")
    repo.get_job.assert_awaited_once_with("j1")
    assert result == "job-stub"


# ---------------------------------------------------------------------------
# mark_step / mark_completed / mark_permanently_failed — with publisher
# ---------------------------------------------------------------------------

async def test_mark_step_writes_db_and_publishes(repo, publisher):
    svc = JobService(repo, publisher)
    await svc.mark_step("j1", "separating", 50)

    repo.mark_step.assert_awaited_once_with("j1", "separating", 50)
    publisher.publish_progress.assert_awaited_once_with("j1", "separating", 50)


async def test_mark_completed_writes_db_and_publishes_with_track_id(repo, publisher):
    svc = JobService(repo, publisher)
    await svc.mark_completed("j1", {"track_id": "t-42", "extra": "x"})

    repo.complete_job.assert_awaited_once_with("j1", {"track_id": "t-42", "extra": "x"})
    publisher.publish_completed.assert_awaited_once_with("j1", "t-42")


async def test_mark_completed_handles_missing_track_id(repo, publisher):
    svc = JobService(repo, publisher)
    await svc.mark_completed("j1", {})
    publisher.publish_completed.assert_awaited_once_with("j1", "")


async def test_mark_permanently_failed_writes_db_and_publishes(repo, publisher):
    svc = JobService(repo, publisher)
    await svc.mark_permanently_failed("j1", "boom")

    repo.fail_job_permanently.assert_awaited_once_with("j1", "boom")
    publisher.publish_error.assert_awaited_once_with("j1", "boom")


# ---------------------------------------------------------------------------
# publisher=None — methods must still write to DB without errors
# ---------------------------------------------------------------------------

async def test_mark_step_without_publisher(repo):
    svc = JobService(repo, publisher=None)
    await svc.mark_step("j1", "vad", 100)
    repo.mark_step.assert_awaited_once()


async def test_mark_completed_without_publisher(repo):
    svc = JobService(repo, publisher=None)
    await svc.mark_completed("j1", {"track_id": "t1"})
    repo.complete_job.assert_awaited_once()


async def test_mark_permanently_failed_without_publisher(repo):
    svc = JobService(repo, publisher=None)
    await svc.mark_permanently_failed("j1", "err")
    repo.fail_job_permanently.assert_awaited_once()


# ---------------------------------------------------------------------------
# publisher exceptions must NOT propagate (DB write is the source of truth)
# ---------------------------------------------------------------------------

async def test_publisher_exception_does_not_break_mark_step(repo, publisher):
    publisher.publish_progress.side_effect = RuntimeError("rmq down")
    svc = JobService(repo, publisher)

    await svc.mark_step("j1", "x", 10)  # must not raise

    repo.mark_step.assert_awaited_once()


async def test_publisher_exception_does_not_break_mark_completed(repo, publisher):
    publisher.publish_completed.side_effect = RuntimeError("rmq down")
    svc = JobService(repo, publisher)

    await svc.mark_completed("j1", {"track_id": "t1"})  # must not raise

    repo.complete_job.assert_awaited_once()


async def test_publisher_exception_does_not_break_mark_permanently_failed(repo, publisher):
    publisher.publish_error.side_effect = RuntimeError("rmq down")
    svc = JobService(repo, publisher)

    await svc.mark_permanently_failed("j1", "err")  # must not raise

    repo.fail_job_permanently.assert_awaited_once()
