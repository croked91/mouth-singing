"""FastAPI dependency functions.

These are used with ``Depends()`` in route handlers. The actual singletons
(db connection, qdrant client) are stored on ``app.state`` and set up during
the lifespan in ``main.py``.

Usage in a route::

    @router.get("/example")
    async def example(
        db: aiosqlite.Connection = Depends(get_db),
        repo: SQLiteRepository = Depends(get_sqlite_repo),
        qdrant: QdrantClient = Depends(get_qdrant),
        qdrant_repo: QDrantRepository = Depends(get_qdrant_repo),
    ):
        ...
"""

import aiosqlite
from fastapi import Request
from karaoke_shared import QDrantRepository, SQLiteRepository
from qdrant_client import QdrantClient


def get_db(request: Request) -> aiosqlite.Connection:
    """Return the shared SQLite connection from application state.

    The connection is opened once at startup (lifespan) and shared across
    all requests. aiosqlite handles its own internal locking so this is safe.
    """
    return request.app.state.db


def get_qdrant(request: Request) -> QdrantClient:
    """Return the shared QdrantClient instance from application state.

    The sync QdrantClient is used here. For I/O-bound calls inside async
    route handlers, run them in a thread pool via ``asyncio.to_thread()``.
    """
    return request.app.state.qdrant


def get_sqlite_repo(request: Request) -> SQLiteRepository:
    """Return a SQLiteRepository wrapping the shared connection.

    The repository is constructed per-request but the underlying connection
    is shared (stored on ``app.state.db``).
    """
    return SQLiteRepository(request.app.state.db)


def get_qdrant_repo(request: Request) -> QDrantRepository:
    """Return a QDrantRepository wrapping the shared QdrantClient.

    Calls to this repository inside async handlers should be wrapped in
    ``asyncio.to_thread()`` since the underlying client is synchronous.
    """
    return QDrantRepository(request.app.state.qdrant)


def get_embedder(request: Request):
    """Return the sentence-transformers Embedder, or ``None`` if not loaded.

    The embedder is loaded once at startup and stored on ``app.state``. If it
    was not loaded (missing dependency or failed download), this returns
    ``None`` and the search service falls back to FTS-only mode.
    """
    return getattr(request.app.state, "embedder", None)
