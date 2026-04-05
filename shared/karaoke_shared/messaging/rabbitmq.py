"""RabbitMQ client with topology declaration and pub/sub helpers.

Uses aio-pika for async AMQP communication.

Topology:
    Exchange "jobs" (direct, durable) → Queue "jobs.process" (durable, priority, DLX)
    Exchange "job.progress" (fanout) → exclusive auto-delete queues per SSE subscriber
    Exchange "rec" (direct, durable) → Queue "rec.index" (durable, DLX)
    Exchange "dlq" (direct) → Queues "jobs.dlq", "rec.dlq"
"""

from __future__ import annotations

import json
from typing import Any, Callable, Awaitable

import aio_pika
import structlog

logger = structlog.get_logger(__name__)


class RabbitMQClient:
    """Async RabbitMQ client wrapping aio-pika.

    Args:
        url: AMQP connection URL, e.g. amqp://karaoke:karaoke@rabbitmq:5672/
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None

    async def connect(self) -> None:
        """Establish connection and channel."""
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        logger.info("rabbitmq_connected", url=self._url.split("@")[-1])

    async def close(self) -> None:
        """Close connection gracefully."""
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
            logger.info("rabbitmq_disconnected")

    @property
    def channel(self) -> aio_pika.abc.AbstractChannel:
        if self._channel is None:
            raise RuntimeError("RabbitMQ not connected. Call connect() first.")
        return self._channel

    async def declare_topology(self) -> None:
        """Declare all exchanges, queues, and bindings."""
        ch = self.channel

        # DLQ infrastructure
        dlq_exchange = await ch.declare_exchange(
            "dlq", aio_pika.ExchangeType.DIRECT, durable=True
        )
        await ch.declare_queue("jobs.dlq", durable=True)
        jobs_dlq = await ch.get_queue("jobs.dlq")
        await jobs_dlq.bind(dlq_exchange, routing_key="jobs")

        await ch.declare_queue("rec.dlq", durable=True)
        rec_dlq = await ch.get_queue("rec.dlq")
        await rec_dlq.bind(dlq_exchange, routing_key="rec")

        # Exchange "jobs" (direct) → Queue "jobs.process"
        jobs_exchange = await ch.declare_exchange(
            "jobs", aio_pika.ExchangeType.DIRECT, durable=True
        )
        jobs_queue = await ch.declare_queue(
            "jobs.process",
            durable=True,
            arguments={
                "x-max-priority": 10,
                "x-dead-letter-exchange": "dlq",
                "x-dead-letter-routing-key": "jobs",
            },
        )
        await jobs_queue.bind(jobs_exchange, routing_key="")

        # Exchange "job.progress" (fanout) — no durable queue, SSE creates exclusive ones
        await ch.declare_exchange(
            "job.progress", aio_pika.ExchangeType.FANOUT, durable=False
        )

        # Exchange "rec" (direct) → Queue "rec.index" + Queue "rec.indexed"
        rec_exchange = await ch.declare_exchange(
            "rec", aio_pika.ExchangeType.DIRECT, durable=True
        )
        rec_queue = await ch.declare_queue(
            "rec.index",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "dlq",
                "x-dead-letter-routing-key": "rec",
            },
        )
        await rec_queue.bind(rec_exchange, routing_key="")

        # rec.indexed — rec-service publishes after QDrant upsert,
        # backend consumes to update tracks.qdrant_synced in PG.
        rec_indexed_queue = await ch.declare_queue("rec.indexed", durable=True)
        await rec_indexed_queue.bind(rec_exchange, routing_key="indexed")

        logger.info("rabbitmq_topology_declared")

    async def publish(
        self,
        exchange: str,
        routing_key: str,
        body: dict[str, Any],
        priority: int | None = None,
    ) -> None:
        """Publish a JSON message to an exchange.

        Args:
            exchange: Exchange name.
            routing_key: Routing key (empty string for fanout).
            body: Message body (will be JSON-serialized).
            priority: Optional message priority (0-10).
        """
        ch = self.channel
        ex = await ch.get_exchange(exchange)

        message = aio_pika.Message(
            body=json.dumps(body).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            priority=priority,
        )

        await ex.publish(message, routing_key=routing_key)

    async def consume(
        self,
        queue: str,
        callback: Callable[[aio_pika.abc.AbstractIncomingMessage], Awaitable[None]],
        prefetch_count: int = 1,
    ) -> None:
        """Start consuming messages from a queue.

        Args:
            queue: Queue name to consume from.
            callback: Async function called for each message.
            prefetch_count: Number of unacknowledged messages allowed.
        """
        ch = self.channel
        await ch.set_qos(prefetch_count=prefetch_count)
        q = await ch.get_queue(queue)
        await q.consume(callback)
        logger.info("rabbitmq_consuming", queue=queue, prefetch=prefetch_count)

    async def create_exclusive_queue(self, exchange: str) -> aio_pika.abc.AbstractQueue:
        """Create an exclusive auto-delete queue bound to a fanout exchange.

        Used by SSE endpoints to receive progress updates.

        Args:
            exchange: Fanout exchange name to bind to.

        Returns:
            The exclusive queue (auto-delete on disconnect).
        """
        ch = self.channel
        ex = await ch.get_exchange(exchange)
        queue = await ch.declare_queue(exclusive=True, auto_delete=True)
        await queue.bind(ex)
        return queue
