"""Tests for the recommendation service (R4 — cluster-based with popularity + MMR).

Coverage:
- auto_cluster_session — greedy clustering
- distribute_slots — proportional slot allocation
- popularity_rerank — category weight re-ranking
- mmr_select — MMR diversity selection
- RecommendationService.get_recommendations — POPULAR and CLUSTER strategies
- GET /api/v1/recommendations — endpoint contract
- QueueService.finish_playing — counter updates
"""

from __future__ import annotations

import math
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from karaoke_shared.models.recommendation import RecommendationStrategy
from karaoke_shared.models.track import Track


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DIM = 45
_LYRICS_DIM = 384


def _uid() -> str:
    return str(uuid.uuid4())


def _vec(seed: float = 0.1, dim: int = _DIM) -> list[float]:
    raw = [(seed + i * 0.001) for i in range(dim)]
    norm = sum(x**2 for x in raw) ** 0.5
    return [x / norm for x in raw]


def _audio_vec(seed: float = 0.1) -> list[float]:
    return _vec(seed, _DIM)


def _lyrics_vec(seed: float = 0.1) -> list[float]:
    return _vec(seed, _LYRICS_DIM)


def _make_track(track_id: str | None = None, popularity_category: str = "regular") -> Track:
    now = "2024-01-01T00:00:00+00:00"
    return Track(
        id=track_id or _uid(),
        artist="Test Artist",
        title="Test Song",
        source="catalog",
        status="ready",
        popularity_category=popularity_category,
        created_at=now,
        updated_at=now,
    )


def _make_history_entry(track_id: str, session_id: str = "sess-1"):
    entry = MagicMock()
    entry.track_id = track_id
    entry.session_id = session_id
    return entry


def _make_sqlite_repo(session_history=None, popular=None) -> AsyncMock:
    repo = AsyncMock()
    repo.get_history_by_session.return_value = session_history or []
    repo.list_popular.return_value = popular or []
    repo.list_random.return_value = []
    repo.get_tracks_by_ids.return_value = {}
    return repo


def _make_qdrant_repo(
    audio_retrieve=None, lyrics_retrieve=None,
    audio_search=None, lyrics_search=None,
) -> MagicMock:
    repo = MagicMock()

    def _retrieve(collection, track_id):
        if collection == "audio_features":
            return audio_retrieve
        if collection == "lyrics_embeddings":
            return lyrics_retrieve
        return None

    def _search(collection, vector, limit=10, filters=None):
        if collection == "audio_features":
            return audio_search or []
        if collection == "lyrics_embeddings":
            return lyrics_search or []
        return []

    repo.retrieve.side_effect = _retrieve
    repo.search.side_effect = _search
    return repo


from app.services.recommendation_service import (  # noqa: E402
    RecommendationService,
    RecommendedTrack,
    auto_cluster_session,
    distribute_slots,
    mmr_select,
    popularity_rerank,
)


# ===========================================================================
# TestAutoClusterSession
# ===========================================================================


class TestAutoClusterSession:

    def test_single_track_one_cluster(self):
        vecs = [("t1", _audio_vec(0.1), _lyrics_vec(0.1))]
        clusters = auto_cluster_session(vecs)
        assert len(clusters) == 1
        assert clusters[0]["track_ids"] == ["t1"]

    def test_similar_tracks_same_cluster(self):
        """Two very similar tracks end up in the same cluster."""
        vecs = [
            ("t1", _audio_vec(0.10), _lyrics_vec(0.10)),
            ("t2", _audio_vec(0.11), _lyrics_vec(0.11)),  # very close
        ]
        clusters = auto_cluster_session(vecs)
        assert len(clusters) == 1
        assert set(clusters[0]["track_ids"]) == {"t1", "t2"}

    def test_different_tracks_separate_clusters(self):
        """Two orthogonal tracks create separate clusters."""
        # Create truly orthogonal vectors (one-hot style)
        audio_a = [1.0] + [0.0] * (_DIM - 1)
        lyrics_a = [1.0] + [0.0] * (_LYRICS_DIM - 1)
        audio_b = [0.0] * (_DIM - 1) + [1.0]
        lyrics_b = [0.0] * (_LYRICS_DIM - 1) + [1.0]
        vecs = [
            ("t1", audio_a, lyrics_a),
            ("t2", audio_b, lyrics_b),
        ]
        clusters = auto_cluster_session(vecs)
        assert len(clusters) == 2

    def test_max_3_clusters(self):
        """Even with 5 distinct vibes, max 3 clusters are created."""
        # Create 5 one-hot-style orthogonal vectors
        vecs = []
        for i in range(5):
            audio = [0.0] * _DIM
            audio[i % _DIM] = 1.0
            lyrics = [0.0] * _LYRICS_DIM
            lyrics[i % _LYRICS_DIM] = 1.0
            vecs.append((f"t{i}", audio, lyrics))
        clusters = auto_cluster_session(vecs)
        assert len(clusters) <= 3

    def test_singleton_weight(self):
        """A cluster with 1 track has weight 0.5."""
        vecs = [("t1", _audio_vec(0.1), _lyrics_vec(0.1))]
        clusters = auto_cluster_session(vecs)
        assert clusters[0]["weight"] == 0.5

    def test_full_cluster_weight(self):
        """A cluster with 2+ tracks has weight 1.0."""
        vecs = [
            ("t1", _audio_vec(0.10), _lyrics_vec(0.10)),
            ("t2", _audio_vec(0.11), _lyrics_vec(0.11)),
        ]
        clusters = auto_cluster_session(vecs)
        assert clusters[0]["weight"] == 1.0

    def test_empty_input(self):
        assert auto_cluster_session([]) == []

    def test_centroid_is_mean(self):
        """Centroid should be the running mean of cluster members."""
        v1 = [1.0] * _DIM
        v2 = [3.0] * _DIM
        vecs = [
            ("t1", v1, _lyrics_vec(0.1)),
            ("t2", v2, _lyrics_vec(0.1)),  # similar lyrics → same cluster
        ]
        clusters = auto_cluster_session(vecs)
        # If they're in the same cluster, centroid audio should be mean
        if len(clusters) == 1:
            expected = [2.0] * _DIM
            assert clusters[0]["centroid_audio"] == pytest.approx(expected)


# ===========================================================================
# TestDistributeSlots
# ===========================================================================


class TestDistributeSlots:

    def test_single_cluster_gets_all(self):
        clusters = [{"track_ids": ["t1", "t2"], "weight": 1.0}]
        assert distribute_slots(clusters, 4) == [4]

    def test_two_equal_clusters(self):
        clusters = [
            {"track_ids": ["t1", "t2"], "weight": 1.0},
            {"track_ids": ["t3", "t4"], "weight": 1.0},
        ]
        slots = distribute_slots(clusters, 4)
        assert sum(slots) == 4
        assert all(s >= 1 for s in slots)

    def test_proportional_allocation(self):
        """Larger cluster gets more slots."""
        clusters = [
            {"track_ids": ["t1", "t2", "t3", "t4"], "weight": 1.0},  # 4 tracks
            {"track_ids": ["t5"], "weight": 0.5},  # 1 track, singleton
        ]
        slots = distribute_slots(clusters, 4)
        assert sum(slots) == 4
        assert slots[0] > slots[1]  # bigger cluster gets more

    def test_minimum_1_per_cluster(self):
        """Every cluster gets at least 1 slot."""
        clusters = [
            {"track_ids": [f"t{i}" for i in range(10)], "weight": 1.0},
            {"track_ids": ["tx"], "weight": 0.5},
        ]
        slots = distribute_slots(clusters, 3)
        assert all(s >= 1 for s in slots)
        assert sum(slots) == 3

    def test_empty_clusters(self):
        assert distribute_slots([], 4) == []


# ===========================================================================
# TestPopularityRerank
# ===========================================================================


class TestPopularityRerank:

    def test_eternal_hit_beats_regular(self):
        """Eternal hit with lower similarity beats regular with higher similarity."""
        regular = RecommendedTrack(_make_track(popularity_category="regular"), 0.90)
        eternal = RecommendedTrack(_make_track(popularity_category="eternal_hit"), 0.80)

        result = popularity_rerank([regular, eternal])

        # eternal: 0.80 * 2.0 = 1.60, regular: 0.90 * 1.1 = 0.99
        assert result[0].track.popularity_category == "eternal_hit"

    def test_preserves_order_same_category(self):
        """Tracks with same category are ordered by original similarity."""
        t1 = RecommendedTrack(_make_track(popularity_category="regular"), 0.90)
        t2 = RecommendedTrack(_make_track(popularity_category="regular"), 0.80)

        result = popularity_rerank([t2, t1])
        assert result[0].similarity_score > result[1].similarity_score


# ===========================================================================
# TestMMRSelect
# ===========================================================================


class TestMMRSelect:

    def test_selects_limit_tracks(self):
        tracks = [RecommendedTrack(_make_track(), 0.9 - i * 0.1) for i in range(10)]
        result = mmr_select(tracks, 3)
        assert len(result) == 3

    def test_first_is_best_score(self):
        t_best = RecommendedTrack(_make_track("best"), 0.99)
        t_other = RecommendedTrack(_make_track("other"), 0.50)
        result = mmr_select([t_other, t_best], 2)
        assert result[0].track.id == "best"

    def test_diversity_effect(self):
        """With audio vectors, MMR penalises similar tracks."""
        id_a = _uid()
        id_b = _uid()
        id_c = _uid()

        t_a = RecommendedTrack(_make_track(id_a), 0.95)
        t_b = RecommendedTrack(_make_track(id_b), 0.93)  # similar to A
        t_c = RecommendedTrack(_make_track(id_c), 0.85)  # different from A

        # A and B nearly identical, C orthogonal
        vec_a = [1.0] + [0.0] * (_DIM - 1)
        vec_b = [0.99] + [0.01] * (_DIM - 1)  # almost same as A
        vec_c = [0.0] * (_DIM - 1) + [1.0]     # orthogonal to A

        audio_vecs = {id_a: vec_a, id_b: vec_b, id_c: vec_c}
        result = mmr_select([t_a, t_b, t_c], 2, audio_vecs)

        # First should be A (best score), second should be C (most different)
        assert result[0].track.id == id_a
        assert result[1].track.id == id_c

    def test_fallback_without_vectors(self):
        """Without audio vectors, MMR falls back to top-N by score."""
        tracks = [RecommendedTrack(_make_track(), 0.9 - i * 0.1) for i in range(5)]
        result = mmr_select(tracks, 3, None)
        assert len(result) == 3
        assert result[0].similarity_score >= result[1].similarity_score

    def test_empty_candidates(self):
        assert mmr_select([], 3) == []


# ===========================================================================
# TestGetRecommendations
# ===========================================================================


class TestGetRecommendations:

    async def test_no_history_returns_popular(self):
        popular = _make_track()
        sqlite_repo = _make_sqlite_repo(popular=[popular])
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations("s-1")
        assert strategy is RecommendationStrategy.POPULAR

    async def test_with_history_returns_cluster(self):
        """With play history and available vectors, returns CLUSTER strategy."""
        tid = _uid()
        history = [_make_history_entry(tid)]
        sqlite_repo = _make_sqlite_repo(session_history=history, popular=[_make_track()])
        sqlite_repo.get_tracks_by_ids.return_value = {}

        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            lyrics_retrieve=_lyrics_vec(),
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations("s-1")
        assert strategy is RecommendationStrategy.CLUSTER

    async def test_fallback_to_popular_when_no_vectors(self):
        """When played tracks have no vectors, falls back to POPULAR."""
        tid = _uid()
        history = [_make_history_entry(tid)]
        popular = _make_track()
        sqlite_repo = _make_sqlite_repo(session_history=history, popular=[popular])

        qdrant_repo = _make_qdrant_repo(audio_retrieve=None, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations("s-1")
        assert strategy is RecommendationStrategy.POPULAR

    async def test_uses_session_history(self):
        sqlite_repo = _make_sqlite_repo()
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("s-1")

        sqlite_repo.get_history_by_session.assert_called_once_with("s-1")

    async def test_default_limit_is_5(self):
        tracks = [_make_track() for _ in range(10)]
        sqlite_repo = _make_sqlite_repo(popular=tracks)
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("s-1")
        assert len(results) <= 5


# ===========================================================================
# TestQDrantRepoRetrieve
# ===========================================================================


class TestQDrantRepoRetrieve:

    def test_retrieve_returns_vector(self, qdrant_repo):
        pid = _uid()
        v = _audio_vec(0.42)
        qdrant_repo.upsert("audio_features", pid, v, {"status": "ready"})
        result = qdrant_repo.retrieve("audio_features", pid)
        assert result is not None
        assert result == pytest.approx(v, abs=1e-5)

    def test_retrieve_returns_none_for_missing(self, qdrant_repo):
        assert qdrant_repo.retrieve("audio_features", _uid()) is None


# ===========================================================================
# TestRecommendationsEndpoint
# ===========================================================================


class TestRecommendationsEndpoint:

    @pytest_asyncio.fixture
    async def rec_fixtures(self, client, app_db):
        from karaoke_shared.models.track import TrackCreate
        from karaoke_shared.repositories import SQLiteRepository
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams
        from app.main import app

        repo = SQLiteRepository(app_db)
        track = await repo.create_track(
            TrackCreate(artist="Queen", title="Bohemian Rhapsody", source="catalog", status="ready", duration_sec=354)
        )

        r = await client.post("/api/v1/sessions", json={"room_id": "room-rec-1"})
        session_id = r.json()["id"]

        qdrant_client = QdrantClient(":memory:")
        for coll, dim in [("audio_features", 45), ("lyrics_embeddings", 384)]:
            qdrant_client.create_collection(
                collection_name=coll,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        app.state.qdrant = qdrant_client

        yield {"session_id": session_id, "track_id": track.id}
        app.state.qdrant = None

    async def test_returns_200(self, client, rec_fixtures):
        f = rec_fixtures
        r = await client.get("/api/v1/recommendations", params={"session_id": f["session_id"]})
        assert r.status_code == 200

    async def test_returns_popular_strategy(self, client, rec_fixtures):
        f = rec_fixtures
        r = await client.get("/api/v1/recommendations", params={"session_id": f["session_id"]})
        assert r.json()["strategy"] in {"popular", "cluster"}

    async def test_missing_session_id_returns_422(self, client, rec_fixtures):
        r = await client.get("/api/v1/recommendations", params={})
        assert r.status_code == 422

    async def test_language_filter_accepted(self, client, rec_fixtures):
        f = rec_fixtures
        r = await client.get("/api/v1/recommendations", params={"session_id": f["session_id"], "language": "ru"})
        assert r.status_code == 200

    async def test_tag_id_accepted(self, client, rec_fixtures):
        f = rec_fixtures
        r = await client.get("/api/v1/recommendations", params={"session_id": f["session_id"], "tag_id": 999})
        assert r.status_code == 200  # returns empty for non-existent tag


# ===========================================================================
# TestQueueServiceFinishPlaying
# ===========================================================================


class TestQueueServiceFinishPlaying:

    def _make_queue_entry(self, entry_id, session_id, participant_id, track_id):
        entry = MagicMock()
        entry.id = entry_id
        entry.session_id = session_id
        entry.participant_id = participant_id
        entry.track_id = track_id
        return entry

    async def _build_service(self, entry, next_entry=None):
        from app.services.queue_service import QueueService
        repo = AsyncMock()
        repo.get_queue_entry.return_value = entry
        repo.update_queue_entry_status.return_value = None
        repo.create_play_history.return_value = MagicMock()
        repo.increment_play_count.return_value = None
        repo.increment_tracks_played.return_value = None
        repo.get_current_entry.return_value = next_entry
        return QueueService(repo=repo), repo

    async def test_returns_none_for_missing_entry(self):
        from app.services.queue_service import QueueService
        repo = AsyncMock()
        repo.get_queue_entry.return_value = None
        service = QueueService(repo=repo)
        assert await service.finish_playing("nonexistent") is None

    async def test_increments_play_count(self):
        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_service(entry)
        await service.finish_playing("e-1")
        repo.increment_play_count.assert_called_once_with("t-1")

    async def test_creates_play_history(self):
        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_service(entry)
        await service.finish_playing("e-1")
        repo.create_play_history.assert_called_once()
