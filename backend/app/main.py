"""FastAPI application entry point.

Startup sequence (managed by the lifespan context manager):
1. Configure structlog JSON logging.
2. Connect to PostgreSQL and apply the schema.
3. Connect to QDrant and ensure the required collections exist.

The app is designed to run as a kiosk service in a karaoke room, so CORS is
open to all origins — there is no public internet exposure.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

from app.api.router import v1_router
from app.config import settings
from app.db import init_pg
from app.logging_config import configure_logging
from karaoke_shared.messaging import RabbitMQClient
from karaoke_shared.storage import S3Storage
from karaoke_shared.constants import (
    AUDIO_FEATURE_DIM,
    COLLECTION_AUDIO_FEATURES,
    COLLECTION_LYRICS_EMBEDDINGS,
    LYRICS_EMBEDDING_DIM,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# QDrant collection definitions
# ---------------------------------------------------------------------------

_QDRANT_COLLECTIONS: list[tuple[str, int, Distance]] = [
    (COLLECTION_AUDIO_FEATURES, AUDIO_FEATURE_DIM, Distance.COSINE),
    (COLLECTION_LYRICS_EMBEDDINGS, LYRICS_EMBEDDING_DIM, Distance.COSINE),
]

# Payload fields to index for efficient filtered searches.
_PAYLOAD_INDEXES: list[str] = ["status", "language", "source"]


def _ensure_qdrant_collections(client: QdrantClient) -> None:
    """Create QDrant collections and payload indexes if they do not exist."""
    existing = {c.name for c in client.get_collections().collections}

    for name, dim, distance in _QDRANT_COLLECTIONS:
        if name in existing:
            logger.info("qdrant_collection_exists", collection=name)
            continue

        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dim, distance=distance),
        )
        logger.info("qdrant_collection_created", collection=name, dim=dim)

        for field in _PAYLOAD_INDEXES:
            client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info(
                "qdrant_payload_index_created",
                collection=name,
                field=field,
            )



# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of shared resources."""
    # 1. Logging must be configured first so all subsequent messages are JSON.
    configure_logging()
    logger.info("karaoke_backend_starting", log_level=settings.log_level)

    # 2. PostgreSQL — create pool and apply schema.
    pool = await init_pg(settings.pg_dsn)
    app.state.pg_pool = pool

    # 3. S3 storage — create client and ensure bucket exists.
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

    # 4. RabbitMQ — connect and declare topology.
    rmq = RabbitMQClient(settings.rabbitmq_url)
    try:
        await rmq.connect()
        await rmq.declare_topology()
        app.state.rmq = rmq
    except Exception as exc:
        logger.warning("rabbitmq_init_failed", error=str(exc))
        app.state.rmq = None

    # 5. QDrant — create client and ensure collections exist.
    qdrant = QdrantClient(
        host=settings.qdrant_host, port=settings.qdrant_port, timeout=10
    )
    app.state.qdrant = qdrant

    # Collection creation is sync; run it off the event loop thread.
    try:
        await asyncio.to_thread(_ensure_qdrant_collections, qdrant)
    except Exception as exc:
        logger.warning("qdrant_init_failed", error=str(exc))

    # 5. Sentence-transformer embedder for semantic search (optional).
    embedder = None
    try:
        from app.services.embedder import Embedder  # noqa: PLC0415

        embedder = Embedder()
        logger.info(
            "embedder_loaded",
            model="paraphrase-multilingual-MiniLM-L12-v2",
        )
    except Exception as exc:
        logger.warning("embedder_not_available", error=str(exc))
    app.state.embedder = embedder

    logger.info("karaoke_backend_ready")

    yield

    # Shutdown: close connections cleanly.
    logger.info("karaoke_backend_shutting_down")
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

# The /health endpoint lives at the root (no /api/v1 prefix) so the Docker
# health-check can hit it without knowing the API version.
from app.api.v1 import health as health_module  # noqa: E402 — avoids circular import

app.include_router(health_module.router)
