from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
from pydantic import BaseModel

from app.dependencies import get_sqlite_repo

logger = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ActiveJobResponse(BaseModel):
    """A currently active (pending/running) upload job with track metadata."""

    job_id: str
    track_id: str
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
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> list[ActiveJobResponse]:
    """Return all pending/running jobs for user-uploaded tracks."""
    jobs = await repo.get_active_upload_jobs()
    results: list[ActiveJobResponse] = []
    for job in jobs:
        track = await repo.get_track(job.track_id)
        results.append(
            ActiveJobResponse(
                job_id=job.id,
                track_id=job.track_id,
                status=job.status,
                current_step=job.current_step,
                progress=job.progress,
                artist=track.artist if track else "Unknown",
                title=track.title if track else "Unknown",
            )
        )
    return results

# Maximum time (seconds) to keep an SSE stream open before closing it.
_STREAM_TIMEOUT_SEC = 300

# How often (seconds) to poll SQLite for job status changes.
_POLL_INTERVAL_SEC = 2


@router.get("/jobs/{job_id}/status")
async def job_status_stream(
    job_id: str,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> StreamingResponse:
    """Server-Sent Events stream for job processing status.

    Emits events as the job progresses through pipeline steps.  The stream
    closes automatically when the job reaches a terminal state (completed or
    failed) or after a 5-minute safety timeout.

    Events:
        status    — emitted when current_step or progress changes.
        completed — emitted once when the job finishes successfully.
        error     — emitted on failure or when the job is not found.

    Args:
        job_id: The job ID to stream status for.
        repo: SQLite repository dependency.
    """

    async def event_generator():
        elapsed = 0
        last_step: str | None = None
        last_progress: int | None = None

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

                payload = json.dumps(
                    {
                        "job_id": job_id,
                        "status": "completed",
                        "track_id": job.track_id,
                        "clip_url": stream_url,
                    }
                )
                yield f"event: completed\ndata: {payload}\n\n"
                return

            if job.status == "failed":
                error_message = job.error_message or "Unknown error"
                payload = json.dumps(
                    {"job_id": job_id, "status": "failed", "error": error_message}
                )
                yield f"event: error\ndata: {payload}\n\n"
                return

            # Emit a status event only when something has changed.
            if job.current_step != last_step or job.progress != last_progress:
                last_step = job.current_step
                last_progress = job.progress
                payload = json.dumps(
                    {
                        "job_id": job_id,
                        "status": job.status,
                        "step": job.current_step,
                        "progress": job.progress,
                    }
                )
                yield f"event: status\ndata: {payload}\n\n"

            await asyncio.sleep(_POLL_INTERVAL_SEC)
            elapsed += _POLL_INTERVAL_SEC

        # Timeout reached — emit a final error and close.
        payload = json.dumps(
            {"job_id": job_id, "status": "timeout", "error": "Stream timed out"}
        )
        yield f"event: error\ndata: {payload}\n\n"
        logger.warning("sse_stream_timeout", job_id=job_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
