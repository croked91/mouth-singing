"""Shared fixtures for the karaoke data layer test suite.

Fixtures:
- sqlite_db: in-memory aiosqlite connection with full schema applied
- sqlite_repo: SQLiteRepository wrapping sqlite_db
- qdrant_client_memory: QdrantClient(":memory:") with 3 collections
- qdrant_repo: QDrantRepository wrapping qdrant_client_memory
- app_db: in-memory aiosqlite connection wired into app.state.db
- client: httpx.AsyncClient talking to the FastAPI app via ASGITransport
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from karaoke_shared.repositories import QDrantRepository, SQLiteRepository

# Ensure the backend package is importable when running from the project root.
_BACKEND_DIR = pathlib.Path(__file__).parent.parent / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Path to the canonical SQL schema used by the application.
_INIT_SQL = (
    pathlib.Path(__file__).parent.parent
    / "backend"
    / "app"
    / "db"
    / "init.sql"
)

# Collection definitions: name -> vector dimension
_QDRANT_COLLECTIONS: dict[str, int] = {
    "audio_features": 45,
    "lyrics_embeddings": 384,
    "transitions": 45,
}


# ---------------------------------------------------------------------------
# SQLite fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sqlite_db():
    """Open an in-memory aiosqlite connection and apply the full schema.

    The connection has row_factory set so that rows can be accessed by column
    name (required by SQLiteRepository._row_to_dict).
    """
    schema = _INIT_SQL.read_text()
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        # Execute each statement separately; executescript commits implicitly
        # and is incompatible with WAL mode, but works fine in test context.
        await db.executescript(schema)
        yield db


@pytest_asyncio.fixture
async def sqlite_repo(sqlite_db):
    """Return a SQLiteRepository backed by the in-memory database."""
    return SQLiteRepository(sqlite_db)


# ---------------------------------------------------------------------------
# QDrant fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def qdrant_client_memory():
    """Return a QdrantClient using in-memory storage with 3 collections."""
    client = QdrantClient(":memory:")
    for collection_name, dimension in _QDRANT_COLLECTIONS.items():
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
        )
    return client


@pytest.fixture
def qdrant_repo(qdrant_client_memory):
    """Return a QDrantRepository wrapping the in-memory QdrantClient."""
    return QDrantRepository(qdrant_client_memory)


# ---------------------------------------------------------------------------
# FastAPI integration fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_db():
    """Open an in-memory aiosqlite connection and wire it into app.state.db.

    The connection is also stored on the fixture so that queue tests can
    insert seed data (tracks) directly via SQLiteRepository.

    A fresh database is created for each test, ensuring full isolation.
    """
    import os

    # Ensure the admin secret is set before importing the app module so that
    # pydantic_settings picks up the value from the environment.
    os.environ.setdefault("ADMIN_SECRET", "test-secret")

    from app.main import app  # noqa: PLC0415 — local import to keep import order safe

    schema = _INIT_SQL.read_text()
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(schema)
        # Inject the DB directly into app.state, bypassing the lifespan.
        # QDrant is not needed for session/queue endpoints.
        app.state.db = db
        app.state.qdrant = None
        yield db
        # Reset state so that subsequent tests don't share it.
        del app.state.db
        del app.state.qdrant


@pytest_asyncio.fixture
async def client(app_db):
    """Async HTTP client backed by the FastAPI ASGI app.

    Uses httpx.ASGITransport so no real network is involved.  The app.state.db
    is set by the ``app_db`` fixture before this client is created.
    """
    import os

    os.environ.setdefault("ADMIN_SECRET", "test-secret")

    from app.main import app  # noqa: PLC0415

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as http_client:
        yield http_client
