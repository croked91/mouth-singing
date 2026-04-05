"""FastAPI dependency functions.

These are used with ``Depends()`` in route handlers. The actual singletons
(pg pool, qdrant client) are stored on ``app.state`` and set up during
the lifespan in ``main.py``.

Usage in a route::

    @router.get("/example")
    async def example(
        repo: PgRepository = Depends(get_repo),
        qdrant: QdrantClient = Depends(get_qdrant),
        qdrant_repo: QDrantRepository = Depends(get_qdrant_repo),
        queue_service: QueueService = Depends(get_queue_service),
    ):
        ...
"""

from fastapi import Request
from karaoke_shared import QDrantRepository, PgRepository
from karaoke_shared.storage import S3Storage
from qdrant_client import QdrantClient

from app.services.queue_service import QueueService


def get_repo(request: Request) -> PgRepository:
    """Return a PgRepository wrapping the shared asyncpg pool.

    The pool is created once at startup (lifespan) and shared across
    all requests via ``app.state.pg_pool``.
    """
    return PgRepository(request.app.state.pg_pool)


def get_qdrant(request: Request) -> QdrantClient:
    """Return the shared QdrantClient instance from application state."""
    return request.app.state.qdrant


def get_qdrant_repo(request: Request) -> QDrantRepository:
    """Return a QDrantRepository wrapping the shared QdrantClient."""
    return QDrantRepository(request.app.state.qdrant)


def get_queue_service(request: Request) -> QueueService:
    """Return a QueueService for the current request."""
    repo = get_repo(request)
    return QueueService(repo=repo)


def get_storage(request: Request) -> S3Storage:
    """Return the shared S3Storage instance from application state."""
    return request.app.state.storage


def get_embedder(request: Request):
    """Return the sentence-transformers Embedder, or ``None`` if not loaded."""
    return getattr(request.app.state, "embedder", None)
