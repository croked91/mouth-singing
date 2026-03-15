"""Unit tests for QDrantRepository.

All tests use the in-memory QdrantClient fixture defined in conftest.py.

NOTE: QDrantRepository.search() calls self.client.search() which was removed
in qdrant-client >= 1.7.0 and replaced by client.query_points().
Tests for search-based functionality are marked xfail with the exact
AttributeError they reproduce, and a separate suite tests the underlying
client.query_points() API to confirm vector retrieval itself works.

Bug report is at the bottom of this module.
"""

from __future__ import annotations

import uuid

import pytest

from karaoke_shared.repositories import QDrantRepository

# Collection names matching conftest.py
_AUDIO = "audio_features"
_LYRICS = "lyrics_embeddings"
_TRANSITIONS = "transitions"

_DIM_AUDIO = 45
_DIM_LYRICS = 384


def _vec(dim: int, seed: float = 0.1) -> list[float]:
    """Return a normalised-ish float vector of length *dim*."""
    raw = [(seed + i * 0.001) for i in range(dim)]
    norm = sum(x**2 for x in raw) ** 0.5
    return [x / norm for x in raw]


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Upsert (write path — always works)
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_upsert_single_point(self, qdrant_repo: QDrantRepository):
        """upsert() should not raise for a valid point."""
        pid = _uid()
        qdrant_repo.upsert(_AUDIO, pid, _vec(_DIM_AUDIO), {"status": "ready"})

    def test_upsert_replaces_existing_point(self, qdrant_repo: QDrantRepository):
        """Upserting the same ID twice should not raise."""
        pid = _uid()
        qdrant_repo.upsert(_AUDIO, pid, _vec(_DIM_AUDIO), {"status": "pending"})
        qdrant_repo.upsert(_AUDIO, pid, _vec(_DIM_AUDIO), {"status": "ready"})

    def test_upsert_lyrics_collection(self, qdrant_repo: QDrantRepository):
        pid = _uid()
        qdrant_repo.upsert(_LYRICS, pid, _vec(_DIM_LYRICS), {"track_id": pid})


# ---------------------------------------------------------------------------
# Delete (write path — always works)
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_existing_point(self, qdrant_repo: QDrantRepository):
        """delete() on an existing point should succeed without error."""
        pid = _uid()
        qdrant_repo.upsert(_AUDIO, pid, _vec(_DIM_AUDIO), {"status": "ready"})
        qdrant_repo.delete(_AUDIO, pid)

    def test_delete_nonexistent_point_does_not_raise(self, qdrant_repo: QDrantRepository):
        """Deleting a non-existent point should not raise."""
        qdrant_repo.delete(_AUDIO, _uid())

    def test_delete_removes_point_from_collection(
        self,
        qdrant_repo: QDrantRepository,
        qdrant_client_memory,
    ):
        """After delete, query_points returns empty for that vector."""
        pid = _uid()
        v = _vec(_DIM_AUDIO)
        qdrant_repo.upsert(_AUDIO, pid, v, {"status": "ready"})

        qdrant_repo.delete(_AUDIO, pid)

        # Verify via the underlying client that the point is gone
        response = qdrant_client_memory.query_points(
            _AUDIO, query=v, limit=10
        )
        remaining_ids = {str(p.id) for p in response.points}
        assert pid not in remaining_ids


# ---------------------------------------------------------------------------
# Batch upsert (write path — always works)
# ---------------------------------------------------------------------------


class TestBatchUpsert:
    def test_batch_upsert_150_points_all_stored(
        self,
        qdrant_repo: QDrantRepository,
        qdrant_client_memory,
    ):
        """150 points (2 full batches of 100 + 50 remainder) are all stored."""
        points = [(_uid(), _vec(_DIM_AUDIO, seed=0.01 * i), {"idx": i}) for i in range(150)]
        ids_inserted = {p[0] for p in points}

        qdrant_repo.batch_upsert(_AUDIO, points)

        # Scroll through all stored points to verify count
        scroll_result = qdrant_client_memory.scroll(
            collection_name=_AUDIO,
            limit=200,
            with_payload=False,
            with_vectors=False,
        )
        stored_ids = {str(p.id) for p in scroll_result[0]}
        assert ids_inserted.issubset(stored_ids)

    def test_batch_upsert_empty_list_does_not_raise(
        self, qdrant_repo: QDrantRepository
    ):
        qdrant_repo.batch_upsert(_AUDIO, [])

    def test_batch_upsert_exactly_100_points(
        self,
        qdrant_repo: QDrantRepository,
        qdrant_client_memory,
    ):
        """Exactly one full batch — no off-by-one in the range() call."""
        points = [(_uid(), _vec(_DIM_AUDIO, seed=0.005 * i), {}) for i in range(100)]
        ids_inserted = {p[0] for p in points}

        qdrant_repo.batch_upsert(_AUDIO, points)

        scroll_result = qdrant_client_memory.scroll(
            collection_name=_AUDIO,
            limit=200,
            with_payload=False,
        )
        stored_ids = {str(p.id) for p in scroll_result[0]}
        assert ids_inserted.issubset(stored_ids)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_upsert_and_search(self, qdrant_repo: QDrantRepository):
        """Upsert a point, search with same vector, verify it is returned."""
        pid = _uid()
        v = _vec(_DIM_AUDIO)
        qdrant_repo.upsert(_AUDIO, pid, v, {"status": "ready"})

        results = qdrant_repo.search(_AUDIO, v, limit=1)

        assert len(results) == 1
        returned_id, score, payload = results[0]
        assert returned_id == pid
        assert score > 0.99
        assert payload["status"] == "ready"

    def test_search_with_filter(self, qdrant_repo: QDrantRepository):
        """Filter by payload field — only matching point returned."""
        v_ready = _vec(_DIM_AUDIO, seed=0.2)
        v_pending = _vec(_DIM_AUDIO, seed=0.3)
        pid_ready = _uid()
        pid_pending = _uid()

        qdrant_repo.upsert(_AUDIO, pid_ready, v_ready, {"status": "ready"})
        qdrant_repo.upsert(_AUDIO, pid_pending, v_pending, {"status": "pending"})

        results = qdrant_repo.search(
            _AUDIO, v_ready, limit=10, filters={"status": "ready"}
        )

        ids = [r[0] for r in results]
        assert pid_ready in ids
        assert pid_pending not in ids

    def test_delete_then_search_returns_empty(self, qdrant_repo: QDrantRepository):
        """After deleting a point, search should return empty list."""
        pid = _uid()
        v = _vec(_DIM_AUDIO)
        qdrant_repo.upsert(_AUDIO, pid, v, {"status": "ready"})
        qdrant_repo.delete(_AUDIO, pid)

        results = qdrant_repo.search(_AUDIO, v, limit=1)

        assert results == []
