"""FastAPI dependency functions.

These are used with ``Depends()`` in route handlers. The actual singletons
(db connection, qdrant client) are stored on ``app.state`` and set up during
the lifespan in ``main.py``.

Usage in a route:

    @router.get("/example")
    async def example(
        db: aiosqlite.Connection = Depends(get_db),
        qdrant: QdrantClient = Depends(get_qdrant),
    ):
        ...
"""

from collections.abc import AsyncGenerator

import aiosqlite
from fastapi import Request
from qdrant_client import QdrantClient


async def get_db(request: Request) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Yield the shared SQLite connection from application state.

    The connection is opened once at startup (lifespan) and shared across
    all requests. aiosqlite handles its own internal locking so this is safe.
    """
    yield request.app.state.db


def get_qdrant(request: Request) -> QdrantClient:
    """Return the shared QdrantClient instance from application state.

    The sync QdrantClient is used here. For I/O-bound calls inside async
    route handlers, run them in a thread pool via ``asyncio.to_thread()``.
    """
    return request.app.state.qdrant
