"""FastAPI application entry point.

Startup sequence (managed by the lifespan context manager):
1. Configure structlog JSON logging.
2. Connect to PostgreSQL and apply the schema.
3. Connect to S3 storage.
4. Connect to RabbitMQ and start rec.indexed consumer.
5. Create rec-service HTTP client.
6. Initialize MoodQueryExpander via DeepSeek (optional).
"""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from karaoke_shared.messaging import RabbitMQClient
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.storage import S3Storage

from app.api.router import v1_router
from app.config import settings
from app.db import init_pg
from app.logging_config import configure_logging
from app.middleware import RequestIdMiddleware
from app.services.rec_client import RecClient

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

    # 2. S3 storage (aioboto3 — persistent async client).
    storage = S3Storage(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url or None,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
        presigned_url_base=settings.s3_presigned_url_base or None,
    )
    await storage.connect()
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

    # 5. MoodQueryExpander for mood/theme search (optional, requires DEEPSEEK_API_KEY).
    mood_expander = None
    if settings.deepseek_api_key:
        try:
            from app.services.mood_expander import MoodQueryExpander
            mood_expander = MoodQueryExpander(
                api_key=settings.deepseek_api_key,
                model=settings.deepseek_model,
            )
            logger.info("mood_expander_loaded", model=settings.deepseek_model)
        except Exception as exc:
            logger.warning("mood_expander_not_available", error=str(exc))
    app.state.mood_expander = mood_expander

    # 6. JobSweeper — periodic recovery of orphan pending jobs whose RMQ
    # message was lost. Disabled if RMQ failed to initialise above.
    sweeper = None
    if app.state.rmq is not None:
        from karaoke_shared.repositories.pg_repository import PgRepository
        from karaoke_shared.services.job_service import JobService
        from karaoke_shared.services.progress_publisher import ProgressPublisher

        from app.services.job_sweeper import JobSweeper

        sweeper_repo = PgRepository(pool)
        sweeper_publisher = ProgressPublisher(app.state.rmq)
        sweeper_job_service = JobService(sweeper_repo, publisher=sweeper_publisher)
        sweeper = JobSweeper(
            repo=sweeper_repo,
            rmq=app.state.rmq,
            job_service=sweeper_job_service,
            interval_seconds=settings.sweeper_interval_sec,
            pending_ttl_seconds=settings.sweeper_pending_ttl_sec,
            hard_fail_ttl_seconds=settings.sweeper_hard_fail_ttl_sec,
        )
        await sweeper.start()
    app.state.sweeper = sweeper

    logger.info("karaoke_backend_ready")

    yield

    # Shutdown.
    logger.info("karaoke_backend_shutting_down")
    if sweeper is not None:
        await sweeper.stop()
    await rec_client.close()
    if app.state.rmq:
        await app.state.rmq.close()
    await storage.close()
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
# RequestId comes after CORS so preflight OPTIONS replies still get a stable id.
app.add_middleware(RequestIdMiddleware)

app.include_router(v1_router, prefix="/api/v1")

from app.api.v1 import health as health_module  # noqa: E402

app.include_router(health_module.router)
