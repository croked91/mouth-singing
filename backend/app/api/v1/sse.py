"""SSE endpoints for job progress streaming via RabbitMQ.

Endpoint:
    GET /jobs/{job_id}/status — SSE stream for a specific job
    GET /jobs/active — List all active upload jobs
"""

from __future__ import annotations

import asyncio
import json

import aio_pika
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from karaoke_shared.repositories.pg_repository import PgRepository
from pydantic import BaseModel

from app.dependencies import get_repo

logger = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ActiveJobResponse(BaseModel):
    """A currently active (pending/running) upload job."""

    job_id: str
    track_id: str | None = None
    status: str
    current_step: str | None = None
    progress: int = 0
    artist: str
    title: str


# ---------------------------------------------------------------------------
# Active jobs endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/jobs/active",
    response_model=list[ActiveJobResponse],
    summary="List active upload processing jobs",
)
async def list_active_jobs(
    repo: PgRepository = Depends(get_repo),
) -> list[ActiveJobResponse]:
    """Return all pending/running jobs for user-uploaded tracks."""
    jobs = await repo.get_active_upload_jobs()
    results: list[ActiveJobResponse] = []
    for job in jobs:
        results.append(
            ActiveJobResponse(
                job_id=job.id,
                track_id=job.track_id,
                status=job.status,
                current_step=job.current_step,
                progress=job.progress,
                artist=job.artist_hint or "Unknown",
                title=job.title_hint or "Unknown",
            )
        )
    return results


# Maximum time (seconds) to keep an SSE stream open before closing it.
_STREAM_TIMEOUT_SEC = 300


@router.get("/jobs/{job_id}/status")
async def job_status_stream(
    job_id: str,
    request: Request,
    repo: PgRepository = Depends(get_repo),
) -> StreamingResponse:
    """Server-Sent Events stream for job processing status.

    Subscribes to the RabbitMQ "job.progress" fanout exchange via an
    exclusive auto-delete queue. Falls back to DB polling if RabbitMQ
    is not available.

    Events:
        status    — emitted when current_step or progress changes.
        completed — emitted once when the job finishes successfully.
        error     — emitted on failure or when the job is not found.
    """

    async def event_generator():
        # 1. Check current state from DB (handles reconnection)
        job = await repo.get_job(job_id)
        if job is None:
            payload = json.dumps(
                {"job_id": job_id, "status": "not_found", "error": "Job not found"}
            )
            yield f"event: error\ndata: {payload}\n\n"
            return

        if job.status == "completed":
            stream_url = f"/api/v1/tracks/{job.track_id}/stream"
            payload = json.dumps({
                "job_id": job_id,
                "status": "completed",
                "track_id": job.track_id,
                "clip_url": stream_url,
            })
            yield f"event: completed\ndata: {payload}\n\n"
            return

        if job.status == "failed":
            payload = json.dumps({
                "job_id": job_id,
                "status": "failed",
                "error": job.error_message or "Unknown error",
            })
            yield f"event: error\ndata: {payload}\n\n"
            return

        # Emit current state
        if job.current_step:
            payload = json.dumps({
                "job_id": job_id,
                "status": job.status,
                "step": job.current_step,
                "progress": job.progress,
            })
            yield f"event: status\ndata: {payload}\n\n"

        # 2. Subscribe to RabbitMQ progress events
        rmq = getattr(request.app.state, "rmq", None)
        if rmq is None:
            # Fallback: DB polling if RabbitMQ not available
            async for event in _db_poll_generator(repo, job_id):
                yield event
            return

        try:
            queue = await rmq.create_exclusive_queue("job.progress")
        except Exception as exc:
            logger.warning("rmq_subscribe_failed", error=str(exc))
            async for event in _db_poll_generator(repo, job_id):
                yield event
            return

        # Consume from the exclusive queue, filtering by job_id.
        # We use a callback-based consumer + asyncio.Queue because
        # aio-pika's queue.iterator(timeout=N) closes permanently
        # after the first timeout, breaking long-lived SSE streams.
        buf: asyncio.Queue[aio_pika.abc.AbstractIncomingMessage] = asyncio.Queue()
        consumer_tag = await queue.consume(buf.put)

        try:
            elapsed = 0.0
            while elapsed < _STREAM_TIMEOUT_SEC:
                try:
                    message = await asyncio.wait_for(buf.get(), timeout=5)
                except asyncio.TimeoutError:
                    elapsed += 5
                    yield ": keepalive\n\n"
                    continue

                async with message.process():
                    body = json.loads(message.body)
                    if body.get("job_id") != job_id:
                        continue

                    msg_status = body.get("status", "")
                    if msg_status == "completed":
                        yield f"event: completed\ndata: {json.dumps(body)}\n\n"
                        return
                    elif msg_status == "failed":
                        yield f"event: error\ndata: {json.dumps(body)}\n\n"
                        return
                    else:
                        yield f"event: status\ndata: {json.dumps(body)}\n\n"
                        elapsed = 0.0  # reset on activity

            payload = json.dumps(
                {"job_id": job_id, "status": "timeout", "error": "Stream timed out"}
            )
            yield f"event: error\ndata: {payload}\n\n"
            logger.warning("sse_stream_timeout", job_id=job_id)
        finally:
            await queue.cancel(consumer_tag)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/jobs/{job_id}/result",
    summary="Return a completed job result payload",
)
async def get_job_result(
    job_id: str,
    repo: PgRepository = Depends(get_repo),
) -> dict:
    job = await repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Job is not completed yet.")
    return job.result or {}


async def _db_poll_generator(repo: PgRepository, job_id: str):
    """Fallback: poll DB every 2s for job status changes."""
    elapsed = 0
    last_step = None
    last_progress = None

    while elapsed < _STREAM_TIMEOUT_SEC:
        job = await repo.get_job(job_id)
        if job is None:
            payload = json.dumps(
                {"job_id": job_id, "status": "not_found", "error": "Job not found"}
            )
            yield f"event: error\ndata: {payload}\n\n"
            return

        if job.status == "completed":
            stream_url = f"/api/v1/tracks/{job.track_id}/stream"
            payload = json.dumps({
                "job_id": job_id,
                "status": "completed",
                "track_id": job.track_id,
                "clip_url": stream_url,
            })
            yield f"event: completed\ndata: {payload}\n\n"
            return

        if job.status == "failed":
            payload = json.dumps({
                "job_id": job_id,
                "status": "failed",
                "error": job.error_message or "Unknown error",
            })
            yield f"event: error\ndata: {payload}\n\n"
            return

        if job.current_step != last_step or job.progress != last_progress:
            last_step = job.current_step
            last_progress = job.progress
            payload = json.dumps({
                "job_id": job_id,
                "status": job.status,
                "step": job.current_step,
                "progress": job.progress,
            })
            yield f"event: status\ndata: {payload}\n\n"

        await asyncio.sleep(2)
        elapsed += 2

    payload = json.dumps(
        {"job_id": job_id, "status": "timeout", "error": "Stream timed out"}
    )
    yield f"event: error\ndata: {payload}\n\n"
