"""Shared fixtures for the karaoke data layer test suite.

Fixtures:
- sqlite_db: in-memory aiosqlite connection with full schema applied
- sqlite_repo: SQLiteRepository wrapping sqlite_db
- qdrant_client_memory: QdrantClient(":memory:") with 3 collections
- qdrant_repo: QDrantRepository wrapping qdrant_client_memory
"""

from __future__ import annotations

import asyncio
import pathlib

import aiosqlite
import pytest
import pytest_asyncio
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from karaoke_shared.repositories import QDrantRepository, SQLiteRepository

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
