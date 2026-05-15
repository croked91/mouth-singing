"""Tests for ``worker.app.consumer.JobConsumer``.

Covers all branches of ``_on_message``:
  * malformed JSON  → log + nack(requeue=False) (DLQ)
  * lock_job=False  → log + asyncio.sleep + nack(requeue=True)
  * job not found   → log + ack
  * happy path      → pipeline.process + ack
  * pipeline raises → log + nack(requeue=False)
  * request_id binding into structlog contextvars + reset on exit
  * job_id falls back to ``"unknown"`` when message lacks the field
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.contextvars import get_contextvars

from worker.app.consumer import JobConsumer


def _make_message(payload: bytes | str) -> MagicMock:
    """Create an aio_pika.AbstractIncomingMessage stand-in."""
    msg = MagicMock()
    msg.body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()
    return msg


def _make_job(job_id: str = "job-1") -> MagicMock:
    """A pipeline-opaque stand-in for the Job record fetched from PG."""
    job = MagicMock()
    job.id = job_id
    job.mp3_key = f"uploads/{job_id}.mp3"
    return job


@pytest.fixture
def consumer():
    rmq = MagicMock()
    pipeline = MagicMock()
    pipeline.process = AsyncMock()
    repo = MagicMock()
    repo.lock_job = AsyncMock()
    repo.get_job = AsyncMock()
    job_service = MagicMock()
    return JobConsumer(
        rmq=rmq,
        pipeline=pipeline,
        repo=repo,
        job_service=job_service,
        worker_id="test-worker",
    )


async def test_invalid_json_goes_to_dlq(consumer):
    msg = _make_message(b"{ this is not valid JSON ")

    await consumer._on_message(msg)

    msg.nack.assert_awaited_once_with(requeue=False)
    msg.ack.assert_not_called()
    consumer._repo.lock_job.assert_not_called()
    consumer._pipeline.process.assert_not_called()


async def test_invalid_json_with_empty_body(consumer):
    msg = _make_message(b"")

    await consumer._on_message(msg)

    msg.nack.assert_awaited_once_with(requeue=False)


async def test_lock_failure_requeues_with_cooldown(consumer, monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("worker.app.consumer.asyncio.sleep", fake_sleep)

    consumer._repo.lock_job.return_value = False
    msg = _make_message(json.dumps({"job_id": "j1"}))

    await consumer._on_message(msg)

    assert sleeps == [0.5]
    msg.nack.assert_awaited_once_with(requeue=True)
    msg.ack.assert_not_called()
    consumer._pipeline.process.assert_not_called()


async def test_job_not_found_after_lock_acks(consumer):
    consumer._repo.lock_job.return_value = True
    consumer._repo.get_job.return_value = None
    msg = _make_message(json.dumps({"job_id": "j1"}))

    await consumer._on_message(msg)

    msg.ack.assert_awaited_once()
    msg.nack.assert_not_called()
    consumer._pipeline.process.assert_not_called()


async def test_happy_path_runs_pipeline_and_acks(consumer):
    job = _make_job("j1")
    consumer._repo.lock_job.return_value = True
    consumer._repo.get_job.return_value = job
    msg = _make_message(json.dumps({"job_id": "j1"}))

    await consumer._on_message(msg)

    consumer._repo.lock_job.assert_awaited_once_with("j1", "test-worker")
    consumer._pipeline.process.assert_awaited_once_with(job)
    msg.ack.assert_awaited_once()
    msg.nack.assert_not_called()


async def test_pipeline_exception_nacks_to_dlq(consumer):
    consumer._repo.lock_job.return_value = True
    consumer._repo.get_job.return_value = _make_job()
    consumer._pipeline.process.side_effect = RuntimeError("boom")
    msg = _make_message(json.dumps({"job_id": "j1"}))

    await consumer._on_message(msg)

    msg.nack.assert_awaited_once_with(requeue=False)
    msg.ack.assert_not_called()


async def test_request_id_bound_during_processing_then_reset(consumer):
    """Pipeline.process must observe request_id in structlog contextvars,
    and the binding must be reset after _on_message returns."""
    seen: dict[str, object] = {}

    async def capture_ctx(_job):
        seen.update(get_contextvars())

    consumer._pipeline.process.side_effect = capture_ctx
    consumer._repo.lock_job.return_value = True
    consumer._repo.get_job.return_value = _make_job("j1")
    msg = _make_message(json.dumps({"job_id": "j1", "request_id": "req-abc"}))

    await consumer._on_message(msg)

    assert seen.get("job_id") == "j1"
    assert seen.get("request_id") == "req-abc"
    after = get_contextvars()
    assert "job_id" not in after
    assert "request_id" not in after


async def test_request_id_optional(consumer):
    """When the message has no request_id, only job_id is bound."""
    seen: dict[str, object] = {}

    async def capture_ctx(_job):
        seen.update(get_contextvars())

    consumer._pipeline.process.side_effect = capture_ctx
    consumer._repo.lock_job.return_value = True
    consumer._repo.get_job.return_value = _make_job("j1")
    msg = _make_message(json.dumps({"job_id": "j1"}))

    await consumer._on_message(msg)

    assert seen.get("job_id") == "j1"
    assert "request_id" not in seen


async def test_missing_job_id_falls_back_to_unknown(consumer):
    """Body without ``job_id`` still routes through with ``"unknown"`` id —
    the consumer logs and lets the lock attempt fail naturally."""
    consumer._repo.lock_job.return_value = False
    msg = _make_message(json.dumps({}))

    await consumer._on_message(msg)

    consumer._repo.lock_job.assert_awaited_once_with("unknown", "test-worker")
    msg.nack.assert_awaited_once_with(requeue=True)


async def test_contextvars_reset_even_when_pipeline_raises(consumer):
    consumer._repo.lock_job.return_value = True
    consumer._repo.get_job.return_value = _make_job("j1")
    consumer._pipeline.process.side_effect = RuntimeError("boom")
    msg = _make_message(json.dumps({"job_id": "j1", "request_id": "req-xyz"}))

    await consumer._on_message(msg)

    after = get_contextvars()
    assert "request_id" not in after
    assert "job_id" not in after


async def test_stop_sets_running_false(consumer):
    assert consumer._running is True
    consumer.stop()
    assert consumer._running is False
