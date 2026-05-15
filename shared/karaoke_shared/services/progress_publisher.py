"""Progress publisher — emits job progress events via RabbitMQ.

Used by JobService to push real-time updates to SSE endpoints
through the "job.progress" fanout exchange.
"""

from __future__ import annotations

import structlog
from structlog.contextvars import get_contextvars

from karaoke_shared.messaging.rabbitmq import RabbitMQClient

logger = structlog.get_logger(__name__)


def _with_request_id(body: dict) -> dict:
    """Add request_id from structlog contextvars if it's bound.

    The worker consumer binds request_id for the duration of pipeline
    processing, so progress events emitted from inside the pipeline
    automatically carry the same id without threading it through every call.
    """
    request_id = get_contextvars().get("request_id")
    if request_id:
        body["request_id"] = request_id
    return body


class ProgressPublisher:
    """Publishes job progress events to RabbitMQ "job.progress" exchange."""

    def __init__(self, rmq: RabbitMQClient) -> None:
        self._rmq = rmq

    async def publish_progress(
        self, job_id: str, step: str, progress: int
    ) -> None:
        """Emit a progress update event."""
        await self._rmq.publish("job.progress", "", _with_request_id({
            "job_id": job_id,
            "status": "running",
            "step": step,
            "progress": progress,
        }))

    async def publish_completed(
        self, job_id: str, track_id: str
    ) -> None:
        """Emit a job completion event."""
        clip_url = f"/api/v1/tracks/{track_id}/stream"
        await self._rmq.publish("job.progress", "", _with_request_id({
            "job_id": job_id,
            "status": "completed",
            "track_id": track_id,
            "clip_url": clip_url,
        }))

    async def publish_error(
        self, job_id: str, error: str
    ) -> None:
        """Emit a job error event."""
        await self._rmq.publish("job.progress", "", _with_request_id({
            "job_id": job_id,
            "status": "failed",
            "error": error,
        }))
