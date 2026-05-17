"""Rec Service entry point.

Runs two concurrent tasks:
  1. FastAPI HTTP server (uvicorn) for recommendation queries
  2. RabbitMQ consumer for track indexing (rec.index queue)
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
import uvicorn
from qdrant_client import QdrantClient

from app.api import create_app
from app.config import settings
from app.consumer import RecConsumer
from app.indexer import RecIndexer
from karaoke_shared.messaging.rabbitmq import RabbitMQClient
from karaoke_shared.ml.feature_extractor import FeatureExtractor
from karaoke_shared.ml.lyric_embedder import LyricEmbedder
from karaoke_shared.ml.rec_cluster_assigner import RecClusterAssigner
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.storage import S3Storage

logger = structlog.get_logger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def _run_consumer(shutdown_event: asyncio.Event) -> None:
    """Start the RabbitMQ consumer for rec.index queue."""
    rmq = RabbitMQClient(settings.rabbitmq_url)
    await rmq.connect()
    await rmq.declare_topology()

    s3 = S3Storage(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )
    await s3.connect()

    qdrant_client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    qdrant_repo = QDrantRepository(qdrant_client)

    feature_extractor = FeatureExtractor(
        normalization_stats_path=settings.normalization_stats_path or None,
    )
    lyric_embedder = LyricEmbedder(lazy=True)
    cluster_assigner = RecClusterAssigner(
        centroids_path=settings.rec_cluster_centroids_path or None,
    )

    indexer = RecIndexer(
        qdrant_repo=qdrant_repo,
        s3_storage=s3,
        feature_extractor=feature_extractor,
        lyric_embedder=lyric_embedder,
        cluster_assigner=cluster_assigner,
        rmq=rmq,
    )
    consumer = RecConsumer(indexer)

    await rmq.consume("rec.index", consumer.on_message, prefetch_count=1)
    logger.info("rec_service.consuming", queue="rec.index")

    await shutdown_event.wait()

    lyric_embedder.cleanup()
    await rmq.close()
    await s3.close()
    qdrant_client.close()
    logger.info("rec_service.consumer_stopped")


async def _run_http(shutdown_event: asyncio.Event) -> None:
    """Start the FastAPI HTTP server."""
    app = create_app()
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.http_port,
        log_level="warning",  # structlog handles logging
    )
    server = uvicorn.Server(config)

    # Override uvicorn's signal handling — we manage signals ourselves.
    server.install_signal_handlers = lambda: None

    await server.serve()


async def main() -> None:
    _configure_logging()
    logger.info("rec_service.starting")

    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # Run HTTP server and RabbitMQ consumer concurrently.
    await asyncio.gather(
        _run_http(shutdown_event),
        _run_consumer(shutdown_event),
    )

    logger.info("rec_service.stopped")


if __name__ == "__main__":
    asyncio.run(main())
