"""RabbitMQ consumer for the rec.index queue."""

from __future__ import annotations

import json

import aio_pika
import structlog

from app.indexer import RecIndexer

logger = structlog.get_logger(__name__)


class RecConsumer:
    """Consumes messages from the ``rec.index`` queue and delegates to RecIndexer.

    Expected message format::

        {
            "track_id": "<uuid>",
            "mp3_key": "uploads/<uuid>.mp3",
            "lyrics": "full lyrics text..."
        }
    """

    def __init__(self, indexer: RecIndexer) -> None:
        self._indexer = indexer

    async def on_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        """Process a single message from rec.index queue."""
        async with message.process(requeue=False):
            body = message.body.decode()
            log = logger.bind(delivery_tag=message.delivery_tag)

            try:
                data = json.loads(body)
                track_id = data["track_id"]
                mp3_key = data["mp3_key"]
                lyrics = data.get("lyrics", "")
            except (json.JSONDecodeError, KeyError) as exc:
                log.error("rec_consumer.invalid_message", error=str(exc), body=body[:500])
                # Ack invalid messages so they don't block the queue (DLX handles it)
                return

            log = log.bind(track_id=track_id, mp3_key=mp3_key)
            log.info("rec_consumer.processing")

            try:
                await self._indexer.index(track_id, mp3_key, lyrics)
                log.info("rec_consumer.done")
            except Exception:
                log.exception("rec_consumer.index_failed")
                raise  # Will nack due to exception inside process() context
