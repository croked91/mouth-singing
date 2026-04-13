"""FastAPI dependency functions.

These are used with ``Depends()`` in route handlers. The actual singletons
(pg pool, rec_client) are stored on ``app.state`` and set up during
the lifespan in ``main.py``.

Usage in a route::

    @router.get("/example")
    async def example(
        repo: PgRepository = Depends(get_repo),
        queue_service: QueueService = Depends(get_queue_service),
    ):
        ...
"""

from fastapi import Request
from karaoke_shared import PgRepository
from karaoke_shared.storage import S3Storage

from app.services.queue_service import QueueService


def get_repo(request: Request) -> PgRepository:
    """Return a PgRepository wrapping the shared asyncpg pool."""
    return PgRepository(request.app.state.pg_pool)


def get_queue_service(request: Request) -> QueueService:
    """Return a QueueService for the current request."""
    repo = get_repo(request)
    return QueueService(repo=repo)


def get_storage(request: Request) -> S3Storage:
    """Return the shared S3Storage instance from application state."""
    return request.app.state.storage


def get_mood_expander(request: Request):
    """Return MoodQueryExpander, or ``None`` if DeepSeek is not configured."""
    return getattr(request.app.state, "mood_expander", None)
