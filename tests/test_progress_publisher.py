"""Tests for ``karaoke_shared.services.progress_publisher.ProgressPublisher``.

The publisher must:
  * publish JSON bodies with the right shape to the "job.progress" exchange
  * compute clip_url from track_id on completion
  * automatically pick up ``request_id`` from structlog contextvars and add
    it to every body — that mechanism is what stitches progress events to
    the originating backend request
  * NOT add request_id when none is bound
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.contextvars import bind_contextvars, clear_contextvars

from karaoke_shared.services.progress_publisher import ProgressPublisher


@pytest.fixture(autouse=True)
def _clean_contextvars():
    clear_contextvars()
    yield
    clear_contextvars()


@pytest.fixture
def rmq():
    client = MagicMock()
    client.publish = AsyncMock()
    return client


@pytest.fixture
def publisher(rmq):
    return ProgressPublisher(rmq)


async def test_publish_progress_routes_to_job_progress_exchange(publisher, rmq):
    await publisher.publish_progress("j1", "separating", 50)

    rmq.publish.assert_awaited_once_with(
        "job.progress", "",
        {"job_id": "j1", "status": "running", "step": "separating", "progress": 50},
    )


async def test_publish_completed_includes_clip_url(publisher, rmq):
    await publisher.publish_completed("j1", "track-42")

    body = rmq.publish.await_args.args[2]
    assert body["job_id"] == "j1"
    assert body["status"] == "completed"
    assert body["track_id"] == "track-42"
    assert body["clip_url"] == "/api/v1/tracks/track-42/stream"


async def test_publish_error_includes_message(publisher, rmq):
    await publisher.publish_error("j1", "boom!")

    body = rmq.publish.await_args.args[2]
    assert body == {"job_id": "j1", "status": "failed", "error": "boom!"}


async def test_request_id_is_propagated_when_bound(publisher, rmq):
    bind_contextvars(request_id="req-xyz")

    await publisher.publish_progress("j1", "transcribing", 100)
    await publisher.publish_completed("j1", "t1")
    await publisher.publish_error("j1", "fail")

    for call in rmq.publish.await_args_list:
        body = call.args[2]
        assert body["request_id"] == "req-xyz", body


async def test_request_id_omitted_when_not_bound(publisher, rmq):
    await publisher.publish_progress("j1", "transcribing", 100)
    body = rmq.publish.await_args.args[2]
    assert "request_id" not in body
