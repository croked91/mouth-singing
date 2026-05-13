"""Shared fixtures for the karaoke data layer test suite.

Fixtures:
- qdrant_client_memory: QdrantClient(":memory:") with 2 collections
- qdrant_repo: QDrantRepository wrapping qdrant_client_memory
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from karaoke_shared.repositories import QDrantRepository

# Ensure all packages are importable when running from the project root.
_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
_BACKEND_DIR = _PROJECT_ROOT / "backend"
_SHARED_DIR = _PROJECT_ROOT / "shared"
for _p in (_PROJECT_ROOT, _BACKEND_DIR, _SHARED_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Collection definitions: name -> vector dimension
_QDRANT_COLLECTIONS: dict[str, int] = {
    "audio_features": 45,
    "lyrics_embeddings": 384,
}


@pytest.fixture
def qdrant_client_memory():
    """Return a QdrantClient using in-memory storage with 2 collections."""
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
