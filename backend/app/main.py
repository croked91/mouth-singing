"""FastAPI application entry point.

Startup sequence (managed by the lifespan context manager):
1. Configure structlog JSON logging.
2. Connect to PostgreSQL and apply the schema.
3. Connect to RabbitMQ and start rec.indexed consumer.
4. Create rec-service HTTP client.

QDrant is NOT used by the backend — all vector search is handled
by the rec-service microservice.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import v1_router
from app.config import settings
from app.db import init_pg
from app.logging_config import configure_logging
from app.services.rec_client import RecClient
from karaoke_shared.messaging import RabbitMQClient
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.storage import S3Storage

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# rec.indexed consumer — updates qdrant_synced in PG
# ---------------------------------------------------------------------------

async def _start_rec_indexed_consumer(rmq: RabbitMQClient, pool) -> None:
    """Consume rec.indexed messages and update tracks.qdrant_synced in PG."""

    async def _on_message(message) -> None:
        async with message.process(requeue=False):
            try:
                data = json.loads(message.body.decode())
                track_id = data["track_id"]
                rec_cluster_id = data.get("rec_cluster_id")

                repo = PgRepository(pool)
                from karaoke_shared.models.track import TrackUpdate
                await repo.update_track(
                    track_id,
                    TrackUpdate(qdrant_synced=1, rec_cluster_id=rec_cluster_id),
                )
                logger.info("rec_indexed.updated", track_id=track_id, rec_cluster_id=rec_cluster_id)
            except Exception:
                logger.exception("rec_indexed.failed")

    await rmq.consume("rec.indexed", _on_message, prefetch_count=5)
    logger.info("rec_indexed_consumer.started", queue="rec.indexed")


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of shared resources."""
    configure_logging()
    logger.info("karaoke_backend_starting", log_level=settings.log_level)

    # 1. PostgreSQL — create pool and apply schema.
    pool = await init_pg(settings.pg_dsn)
    app.state.pg_pool = pool

    # 2. S3 storage.
    storage = S3Storage(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url or None,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
        presigned_url_base=settings.s3_presigned_url_base or None,
    )
    try:
        await storage.ensure_bucket()
    except Exception as exc:
        logger.warning("s3_bucket_init_failed", error=str(exc))
    app.state.storage = storage

    # 3. RabbitMQ — connect, declare topology, start rec.indexed consumer.
    rmq = RabbitMQClient(settings.rabbitmq_url)
    try:
        await rmq.connect()
        await rmq.declare_topology()
        app.state.rmq = rmq
        await _start_rec_indexed_consumer(rmq, pool)
    except Exception as exc:
        logger.warning("rabbitmq_init_failed", error=str(exc))
        app.state.rmq = None

    # 4. Rec-service HTTP client.
    rec_client = RecClient(
        base_url=settings.rec_service_url,
        timeout=settings.rec_service_timeout,
    )
    app.state.rec_client = rec_client

    # 5. Sentence-transformer embedder for semantic search (optional).
    embedder = None
    try:
        from app.services.embedder import Embedder
        embedder = Embedder()
        logger.info("embedder_loaded", model="paraphrase-multilingual-MiniLM-L12-v2")
    except Exception as exc:
        logger.warning("embedder_not_available", error=str(exc))
    app.state.embedder = embedder

    logger.info("karaoke_backend_ready")

    yield

    # Shutdown.
    logger.info("karaoke_backend_shutting_down")
    await rec_client.close()
    if app.state.rmq:
        await app.state.rmq.close()
    await pool.close()


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Karaoke API",
    version="0.1.0",
    description="Backend API for the karaoke club application.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router, prefix="/api/v1")

from app.api.v1 import health as health_module  # noqa: E402
app.include_router(health_module.router)
