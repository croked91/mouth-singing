"""Tests for the ``job_queue`` slice of ``PgRepository``.

The asyncpg pool is replaced with an AsyncMock — these are pure smoke
tests that verify each method:
  * issues the right SQL keyword (no full-string match — too brittle to
    every whitespace tweak)
  * passes the documented arguments to asyncpg
  * decodes the affected-rows count from asyncpg's "UPDATE N" / "INSERT N"
    response strings
  * builds the right Job model from a row dict
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from karaoke_shared.constants import JobStatus
from karaoke_shared.models.job import JobCreate
from karaoke_shared.repositories.pg_repository import PgRepository


def _row(**overrides) -> dict:
    """A representative row from job_queue for _job_from_row."""
    base = {
        "id": "j1",
        "track_id": None,
        "mp3_key": "uploads/j1.mp3",
        "artist_hint": "Queen",
        "title_hint": "Bohemian Rhapsody",
        "priority": 5,
        "status": JobStatus.PENDING,
        "locked_by": None,
        "locked_at": None,
        "data": None,
        "result": None,
        "error_message": None,
        "current_step": None,
        "progress": 0,
        "created_at": datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    # asyncpg.Record supports row["field"] and row.get(field). A dict does both.
    return base


@pytest.fixture
def repo():
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock()
    pool.fetch = AsyncMock()
    return PgRepository(pool)


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------

async def test_get_job_returns_none_when_no_row(repo):
    repo.pool.fetchrow.return_value = None
    assert await repo.get_job("missing") is None
    sql = repo.pool.fetchrow.await_args.args[0]
    assert "SELECT" in sql.upper() and "JOB_QUEUE" in sql.upper()
    assert repo.pool.fetchrow.await_args.args[1] == "missing"


async def test_get_job_returns_built_model(repo):
    repo.pool.fetchrow.return_value = _row(id="j1")
    job = await repo.get_job("j1")
    assert job is not None
    assert job.id == "j1"
    assert job.mp3_key == "uploads/j1.mp3"
    assert job.priority == 5


# ---------------------------------------------------------------------------
# lock_job — depends on asyncpg.execute returning "UPDATE 0/1"
# ---------------------------------------------------------------------------

async def test_lock_job_returns_true_when_one_row_updated(repo):
    repo.pool.execute.return_value = "UPDATE 1"
    assert await repo.lock_job("j1", "worker-a") is True

    args = repo.pool.execute.await_args.args
    # status, worker_id, now, job_id, prev_status
    assert args[1] == JobStatus.RUNNING
    assert args[2] == "worker-a"
    assert args[4] == "j1"
    assert args[5] == JobStatus.PENDING


async def test_lock_job_returns_false_when_zero_rows(repo):
    repo.pool.execute.return_value = "UPDATE 0"
    assert await repo.lock_job("j1", "worker-a") is False


# ---------------------------------------------------------------------------
# poll_and_lock — atomic claim
# ---------------------------------------------------------------------------

async def test_poll_and_lock_returns_none_when_no_pending(repo):
    repo.pool.fetchrow.return_value = None
    assert await repo.poll_and_lock("worker-a") is None
    sql = repo.pool.fetchrow.await_args.args[0]
    assert "FOR UPDATE SKIP LOCKED" in sql.upper()


async def test_poll_and_lock_returns_locked_job(repo):
    repo.pool.fetchrow.return_value = _row(
        id="j1", status=JobStatus.RUNNING, locked_by="worker-a",
    )
    job = await repo.poll_and_lock("worker-a")
    assert job is not None
    assert job.id == "j1"
    assert job.status == JobStatus.RUNNING
    assert job.locked_by == "worker-a"


# ---------------------------------------------------------------------------
# complete_job / fail_job_permanently
# ---------------------------------------------------------------------------

async def test_complete_job_writes_status_and_result(repo):
    await repo.complete_job("j1", {"track_id": "t-9"})

    sql = repo.pool.execute.await_args.args[0]
    assert "UPDATE" in sql.upper() and "JOB_QUEUE" in sql.upper()
    args = repo.pool.execute.await_args.args
    assert args[1] == JobStatus.COMPLETED
    # result is JSON-serialized
    assert '"track_id": "t-9"' in args[2]
    assert args[4] == "j1"


async def test_fail_job_permanently_clears_lock(repo):
    await repo.fail_job_permanently("j1", "boom")

    args = repo.pool.execute.await_args.args
    assert args[1] == JobStatus.FAILED
    assert args[2] == "boom"
    assert args[4] == "j1"


# ---------------------------------------------------------------------------
# reset_stale_running_jobs — the result-string parser
# ---------------------------------------------------------------------------

async def test_reset_stale_running_jobs_parses_count(repo):
    repo.pool.execute.return_value = "UPDATE 3"
    count = await repo.reset_stale_running_jobs("worker-a")
    assert count == 3

    args = repo.pool.execute.await_args.args
    assert args[1] == JobStatus.PENDING       # new status
    assert args[3] == JobStatus.RUNNING       # filter old status
    assert args[4] == "worker-a"              # locked_by filter


async def test_reset_stale_running_jobs_zero_when_response_unparseable(repo):
    repo.pool.execute.return_value = "garbage"
    assert await repo.reset_stale_running_jobs("worker-a") == 0


# ---------------------------------------------------------------------------
# create_job — calls INSERT and re-fetches with get_job
# ---------------------------------------------------------------------------

async def test_create_job_inserts_then_refetches(repo):
    repo.pool.fetchrow.return_value = _row(id="j-new")

    data = JobCreate(
        id="j-new",
        track_id=None,
        mp3_key="uploads/j-new.mp3",
        artist_hint="A",
        title_hint="T",
        priority=7,
        status=JobStatus.PENDING,
    )

    job = await repo.create_job(data)

    # INSERT was issued
    sql = repo.pool.execute.await_args.args[0]
    assert "INSERT INTO JOB_QUEUE" in sql.upper()
    # Then get_job was called with the new id
    assert repo.pool.fetchrow.await_args.args[1] == "j-new"
    assert job.id == "j-new"


async def test_create_job_raises_when_refetch_returns_none(repo):
    repo.pool.fetchrow.return_value = None
    data = JobCreate(
        id="j-new", track_id=None, mp3_key="k", artist_hint="A",
        title_hint="T", priority=1, status=JobStatus.PENDING,
    )
    with pytest.raises(RuntimeError, match="not found after insert"):
        await repo.create_job(data)


# ---------------------------------------------------------------------------
# mark_step
# ---------------------------------------------------------------------------

async def test_mark_step_updates_step_and_progress(repo):
    await repo.mark_step("j1", "transcribing", 42)

    sql = repo.pool.execute.await_args.args[0]
    assert "CURRENT_STEP" in sql.upper() and "PROGRESS" in sql.upper()
    args = repo.pool.execute.await_args.args
    assert args[1] == "transcribing"
    assert args[2] == 42
    assert args[4] == "j1"
