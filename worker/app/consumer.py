"""RabbitMQ-based job consumer — replaces DB-polling JobPoller.

Consumes messages from the "jobs.process" queue. Each message contains
{job_id, mp3_key}. The consumer locks the job in the DB, runs the
pipeline, then acks or nacks the message.
"""

from __future__ import annotations

import asyncio
import json

import aio_pika
import structlog

from karaoke_shared.messaging.rabbitmq import RabbitMQClient
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.services.job_service import JobService
from worker.common.base_pipeline import BasePipeline

logger = structlog.get_logger(__name__)


class JobConsumer:
    """Consumes job messages from RabbitMQ and dispatches to the pipeline.

    Args:
        rmq: Connected RabbitMQ client.
        pipeline: Processing pipeline (GPU).
        repo: PostgreSQL repository.
        job_service: Job lifecycle service.
        worker_id: Unique identifier for this worker instance.
    """

    def __init__(
        self,
        rmq: RabbitMQClient,
        pipeline: BasePipeline,
        repo: PgRepository,
        job_service: JobService,
        worker_id: str,
    ) -> None:
        self._rmq = rmq
        self._pipeline = pipeline
        self._repo = repo
        self._job_service = job_service
        self._worker_id = worker_id
        self._running = True

    async def run(self) -> None:
        """Start consuming from the jobs.process queue."""
        await self._rmq.consume(
            "jobs.process",
            self._on_message,
            prefetch_count=1,
        )
        logger.info("job_consumer_started", worker_id=self._worker_id)

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    def stop(self) -> None:
        """Signal the consumer to stop."""
        self._running = False
        logger.info("job_consumer_stopping", worker_id=self._worker_id)

    async def _on_message(
        self, message: aio_pika.abc.AbstractIncomingMessage
    ) -> None:
        """Handle an incoming job message."""
        try:
            body = json.loads(message.body)
            job_id = body["job_id"]
            logger.info("job_received", job_id=job_id, worker_id=self._worker_id)

            # Lock the job in DB
            locked = await self._repo.lock_job(job_id, self._worker_id)
            if not locked:
                logger.warning("job_lock_failed", job_id=job_id)
                await message.nack(requeue=True)
                return

            # Get full job record
            job = await self._repo.get_job(job_id)
            if job is None:
                logger.error("job_not_found_after_lock", job_id=job_id)
                await message.ack()
                return

            # Run the pipeline
            await self._pipeline.process(job)
            await message.ack()
            logger.info("job_completed", job_id=job_id)

        except Exception as exc:
            logger.error(
                "job_processing_failed",
                job_id=body.get("job_id", "unknown") if 'body' in dir() else "unknown",
                error=str(exc),
            )
            # Nack — RabbitMQ will requeue or send to DLQ
            await message.nack(requeue=False)
