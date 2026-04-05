"""Progress publisher — emits job progress events via RabbitMQ.

Used by JobService to push real-time updates to SSE endpoints
through the "job.progress" fanout exchange.
"""

from __future__ import annotations

import structlog

from karaoke_shared.messaging.rabbitmq import RabbitMQClient

logger = structlog.get_logger(__name__)


class ProgressPublisher:
    """Publishes job progress events to RabbitMQ "job.progress" exchange."""

    def __init__(self, rmq: RabbitMQClient) -> None:
        self._rmq = rmq

    async def publish_progress(
        self, job_id: str, step: str, progress: int
    ) -> None:
        """Emit a progress update event."""
        await self._rmq.publish("job.progress", "", {
            "job_id": job_id,
            "status": "running",
            "step": step,
            "progress": progress,
        })

    async def publish_completed(
        self, job_id: str, track_id: str
    ) -> None:
        """Emit a job completion event."""
        clip_url = f"/api/v1/tracks/{track_id}/stream"
        await self._rmq.publish("job.progress", "", {
            "job_id": job_id,
            "status": "completed",
            "track_id": track_id,
            "clip_url": clip_url,
        })

    async def publish_error(
        self, job_id: str, error: str
    ) -> None:
        """Emit a job error event."""
        await self._rmq.publish("job.progress", "", {
            "job_id": job_id,
            "status": "failed",
            "error": error,
        })
