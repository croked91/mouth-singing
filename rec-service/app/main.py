"""Rec Service entry point.

Connects to PostgreSQL, RabbitMQ, S3, QDrant and starts consuming
from the ``rec.index`` queue.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import asyncpg
import structlog
from qdrant_client import QdrantClient

from app.config import settings
from app.consumer import RecConsumer
from app.indexer import RecIndexer
from karaoke_shared.messaging.rabbitmq import RabbitMQClient
from karaoke_shared.ml.feature_extractor import FeatureExtractor
from karaoke_shared.ml.lyric_embedder import LyricEmbedder
from karaoke_shared.ml.rec_cluster_assigner import RecClusterAssigner
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.storage import S3Storage

logger = structlog.get_logger(__name__)


def _configure_logging() -> None:
    """Configure structlog with JSON output."""
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


async def main() -> None:
    """Start the rec service."""
    _configure_logging()
    logger.info("rec_service.starting")

    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("rec_service.shutdown_signal")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # 1. Create asyncpg pool
    pg_pool = await asyncpg.create_pool(settings.pg_dsn)
    logger.info("rec_service.pg_connected")

    # 2. Create RabbitMQ client and declare topology
    rmq = RabbitMQClient(settings.rabbitmq_url)
    await rmq.connect()
    await rmq.declare_topology()
    logger.info("rec_service.rmq_connected")

    # 3. Create S3 storage
    s3 = S3Storage(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )

    # 4. Create QDrant client + repository
    qdrant_client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    qdrant_repo = QDrantRepository(qdrant_client)

    # 5. Load ML components
    feature_extractor = FeatureExtractor(
        normalization_stats_path=settings.normalization_stats_path or None,
    )
    lyric_embedder = LyricEmbedder(lazy=True)
    cluster_assigner = RecClusterAssigner(
        centroids_path=settings.rec_cluster_centroids_path or None,
    )

    # 6. Create indexer and consumer
    pg_repo = PgRepository(pg_pool)
    indexer = RecIndexer(
        pg_repo=pg_repo,
        qdrant_repo=qdrant_repo,
        s3_storage=s3,
        feature_extractor=feature_extractor,
        lyric_embedder=lyric_embedder,
        cluster_assigner=cluster_assigner,
    )
    consumer = RecConsumer(indexer)

    # 7. Start consuming
    await rmq.consume("rec.index", consumer.on_message, prefetch_count=1)
    logger.info("rec_service.consuming", queue="rec.index")

    # 8. Wait for shutdown signal
    await shutdown_event.wait()

    # Graceful shutdown
    logger.info("rec_service.shutting_down")
    lyric_embedder.cleanup()
    await rmq.close()
    await pg_pool.close()
    qdrant_client.close()
    logger.info("rec_service.stopped")


if __name__ == "__main__":
    asyncio.run(main())
