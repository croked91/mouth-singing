"""Tests for the recommendation system (R0 — popular-only, cleaned up).

Coverage:
- RecommendationService.get_recommendations — POPULAR strategy
- _fused_knn_search — weighted fusion scoring (kept for future phases)
- QDrantRepository.retrieve — existing and non-existing point
- GET /api/v1/recommendations — endpoint contract
- QueueService.finish_playing — counter updates (no portrait/transition)

All async tests use asyncio_mode = "auto" (configured in pytest.ini).
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

_DIM = 45  # audio_features vector dimension
_LYRICS_DIM = 384  # lyrics_embeddings vector dimension


def _uid() -> str:
    return str(uuid.uuid4())


def _vec(seed: float = 0.1, dim: int = _DIM) -> list[float]:
    """Return a normalised float vector of dimension dim."""
    raw = [(seed + i * 0.001) for i in range(dim)]
    norm = sum(x**2 for x in raw) ** 0.5
    return [x / norm for x in raw]


def _audio_vec(seed: float = 0.1) -> list[float]:
    return _vec(seed, _DIM)


def _lyrics_vec(seed: float = 0.1) -> list[float]:
    return _vec(seed, _LYRICS_DIM)


def _make_track(track_id: str | None = None) -> Track:
    now = "2024-01-01T00:00:00+00:00"
    return Track(
        id=track_id or _uid(),
        artist="Test Artist",
        title="Test Song",
        source="catalog",
        status="ready",
        created_at=now,
        updated_at=now,
    )


def _make_history_entry(
    participant_id: str,
    track_id: str,
    session_id: str = "sess-1",
):
    entry = MagicMock()
    entry.track_id = track_id
    entry.participant_id = participant_id
    entry.session_id = session_id
    return entry


def _make_sqlite_repo(
    session_history: list | None = None,
    popular: list | None = None,
    track: Track | None = None,
) -> AsyncMock:
    """Build an AsyncMock SQLiteRepository."""
    repo = AsyncMock()
    repo.get_history_by_session.return_value = session_history or []
    repo.list_popular.return_value = popular or []
    repo.list_random.return_value = []
    repo.get_track.return_value = track
    repo.get_tracks_by_ids.return_value = {}
    return repo


def _make_qdrant_repo(
    audio_retrieve: list[float] | None = None,
    lyrics_retrieve: list[float] | None = None,
    audio_search: list | None = None,
    lyrics_search: list | None = None,
) -> MagicMock:
    """Build a MagicMock QDrantRepository."""
    repo = MagicMock()

    def _retrieve_side_effect(collection: str, track_id: str):
        if collection == "audio_features":
            return audio_retrieve
        if collection == "lyrics_embeddings":
            return lyrics_retrieve
        return None

    def _search_side_effect(collection: str, vector, limit=10, filters=None):
        if collection == "audio_features":
            return audio_search or []
        if collection == "lyrics_embeddings":
            return lyrics_search or []
        return []

    repo.retrieve.side_effect = _retrieve_side_effect
    repo.search.side_effect = _search_side_effect
    repo.upsert.return_value = None
    return repo


# ---------------------------------------------------------------------------
# Import RecommendationService (after sys.path is already set up by conftest)
# ---------------------------------------------------------------------------

from app.services.recommendation_service import RecommendationService  # noqa: E402


# ===========================================================================
# TestPopularStrategy
# ===========================================================================


class TestPopularStrategy:
    """Tests for the POPULAR strategy (currently the only strategy)."""

    async def test_returns_popular_strategy(self):
        """get_recommendations always returns POPULAR strategy in R0."""
        popular_track = _make_track()
        sqlite_repo = _make_sqlite_repo(popular=[popular_track])
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, results = await service.get_recommendations("s-1", limit=5)

        assert strategy is RecommendationStrategy.POPULAR

    async def test_returns_popular_tracks(self):
        """Results contain the popular track with similarity_score=0.0."""
        popular_track = _make_track()
        sqlite_repo = _make_sqlite_repo(popular=[popular_track])
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("s-1", limit=5)

        assert len(results) == 1
        assert results[0].track.id == popular_track.id
        assert results[0].similarity_score == 0.0

    async def test_calls_list_popular_and_list_random(self):
        """Both list_popular and list_random are called for the 70/30 mix."""
        popular_tracks = [_make_track() for _ in range(5)]
        random_tracks = [_make_track() for _ in range(5)]
        sqlite_repo = _make_sqlite_repo(popular=popular_tracks)
        sqlite_repo.list_random.return_value = random_tracks
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("s-1", limit=10)

        sqlite_repo.list_popular.assert_called_once()
        sqlite_repo.list_random.assert_called_once()

    async def test_mixes_popular_and_random_slots(self):
        """Results include tracks from both popular and random buckets."""
        popular_id = _uid()
        random_id = _uid()
        sqlite_repo = _make_sqlite_repo(popular=[_make_track(popular_id)])
        sqlite_repo.list_random.return_value = [_make_track(random_id)]
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("s-1", limit=10)

        result_ids = {r.track.id for r in results}
        assert popular_id in result_ids
        assert random_id in result_ids

    async def test_deduplicates_across_buckets(self):
        """A track appearing in both popular and random is included only once."""
        shared_id = _uid()
        shared_track = _make_track(shared_id)
        other_track = _make_track(_uid())

        sqlite_repo = _make_sqlite_repo(popular=[shared_track, other_track])
        sqlite_repo.list_random.return_value = [shared_track]
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("s-1", limit=10)

        result_ids = [r.track.id for r in results]
        assert result_ids.count(shared_id) == 1

    async def test_excludes_played_tracks(self):
        """Popular results exclude tracks already played in the session."""
        played_id = _uid()
        other_id = _uid()
        played_track = _make_track(played_id)
        other_track = _make_track(other_id)

        session_history = [_make_history_entry("p-1", played_id, "s-1")]
        sqlite_repo = _make_sqlite_repo(
            session_history=session_history,
            popular=[played_track, other_track],
        )
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("s-1", limit=10)

        result_ids = {r.track.id for r in results}
        assert played_id not in result_ids
        assert other_id in result_ids

    async def test_default_limit_is_5(self):
        """Default limit is 5 (not 10 as before)."""
        tracks = [_make_track() for _ in range(10)]
        sqlite_repo = _make_sqlite_repo(popular=tracks)
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("s-1")

        assert len(results) <= 5

    async def test_uses_session_history_not_participant(self):
        """get_recommendations uses get_history_by_session, not get_history_by_participant."""
        sqlite_repo = _make_sqlite_repo()
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("s-1", limit=5)

        sqlite_repo.get_history_by_session.assert_called_once_with("s-1")


# ===========================================================================
# TestFusedKnnSearch (kept for future phases R1-R4)
# ===========================================================================


class TestFusedKnnSearch:
    """Tests for _fused_knn_search: weighted fusion scoring."""

    async def test_fusion_score_is_weighted_combination(self):
        """When both audio and lyrics results exist, score = 0.7*audio + 0.3*lyrics."""
        new_id = _uid()
        audio_score = 0.80
        lyrics_score = 0.60
        expected_fused = 0.7 * audio_score + 0.3 * lyrics_score

        sqlite_repo = _make_sqlite_repo()
        sqlite_repo.get_tracks_by_ids.return_value = {new_id: _make_track(new_id)}

        qdrant_repo = _make_qdrant_repo(
            audio_search=[(new_id, audio_score, {})],
            lyrics_search=[(new_id, lyrics_score, {})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        results = await service._fused_knn_search(
            _audio_vec(), _lyrics_vec(), set(), 5
        )

        assert len(results) == 1
        assert results[0].track.id == new_id
        assert results[0].similarity_score == pytest.approx(expected_fused, abs=1e-6)

    async def test_audio_only_fallback(self):
        """When lyrics_vector is None, pure audio score is used."""
        new_id = _uid()
        audio_score = 0.90

        sqlite_repo = _make_sqlite_repo()
        sqlite_repo.get_tracks_by_ids.return_value = {new_id: _make_track(new_id)}

        qdrant_repo = _make_qdrant_repo(
            audio_search=[(new_id, audio_score, {})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        results = await service._fused_knn_search(
            _audio_vec(), None, set(), 5
        )

        assert len(results) == 1
        assert results[0].similarity_score == pytest.approx(audio_score, abs=1e-6)

    async def test_single_modality_normalised_score(self):
        """A track in only one collection gets its raw score (no penalty)."""
        audio_only_id = _uid()
        lyrics_only_id = _uid()

        sqlite_repo = _make_sqlite_repo()
        sqlite_repo.get_tracks_by_ids.return_value = {
            audio_only_id: _make_track(audio_only_id),
            lyrics_only_id: _make_track(lyrics_only_id),
        }

        qdrant_repo = _make_qdrant_repo(
            audio_search=[(audio_only_id, 0.9, {})],
            lyrics_search=[(lyrics_only_id, 0.8, {})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        results = await service._fused_knn_search(
            _audio_vec(), _lyrics_vec(), set(), 5
        )

        result_map = {r.track.id: r.similarity_score for r in results}
        assert result_map[audio_only_id] == pytest.approx(0.9, abs=1e-6)
        assert result_map[lyrics_only_id] == pytest.approx(0.8, abs=1e-6)

    async def test_excludes_played_tracks(self):
        """KNN results exclude tracks in the exclude set."""
        played_id = _uid()
        new_id = _uid()

        sqlite_repo = _make_sqlite_repo()
        sqlite_repo.get_tracks_by_ids.return_value = {new_id: _make_track(new_id)}

        qdrant_repo = _make_qdrant_repo(
            audio_search=[
                (played_id, 0.99, {}),
                (new_id, 0.90, {}),
            ],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        results = await service._fused_knn_search(
            _audio_vec(), None, {played_id}, 5
        )

        result_ids = {r.track.id for r in results}
        assert played_id not in result_ids
        assert new_id in result_ids

    async def test_sorted_by_fused_score_descending(self):
        """Results are ordered by fused score, highest first."""
        id_a = _uid()
        id_b = _uid()

        sqlite_repo = _make_sqlite_repo()
        sqlite_repo.get_tracks_by_ids.return_value = {
            id_a: _make_track(id_a),
            id_b: _make_track(id_b),
        }

        qdrant_repo = _make_qdrant_repo(
            audio_search=[(id_a, 0.95, {}), (id_b, 0.60, {})],
            lyrics_search=[(id_a, 0.50, {}), (id_b, 0.95, {})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        results = await service._fused_knn_search(
            _audio_vec(), _lyrics_vec(), set(), 5
        )

        assert len(results) == 2
        assert results[0].track.id == id_a
        assert results[1].track.id == id_b

    async def test_knn_exception_returns_empty(self):
        """If QDrant search raises, returns empty list."""
        sqlite_repo = _make_sqlite_repo()
        qdrant_repo = _make_qdrant_repo()
        qdrant_repo.search.side_effect = RuntimeError("qdrant unavailable")

        service = RecommendationService(sqlite_repo, qdrant_repo)
        results = await service._fused_knn_search(
            _audio_vec(), None, set(), 5
        )

        assert results == []


# ===========================================================================
# TestQDrantRepoRetrieve — real in-memory QDrant client
# ===========================================================================


class TestQDrantRepoRetrieve:
    """Integration tests for QDrantRepository.retrieve using the real in-memory client."""

    def test_retrieve_returns_vector_for_existing_point(self, qdrant_repo):
        pid = _uid()
        v = _audio_vec(0.42)
        qdrant_repo.upsert("audio_features", pid, v, {"status": "ready"})

        result = qdrant_repo.retrieve("audio_features", pid)

        assert result is not None
        assert len(result) == _DIM
        assert result == pytest.approx(v, abs=1e-5)

    def test_retrieve_returns_none_for_non_existing_point(self, qdrant_repo):
        result = qdrant_repo.retrieve("audio_features", _uid())
        assert result is None

    def test_retrieve_returns_none_after_delete(self, qdrant_repo):
        pid = _uid()
        v = _audio_vec(0.1)
        qdrant_repo.upsert("audio_features", pid, v, {})
        qdrant_repo.delete("audio_features", pid)

        result = qdrant_repo.retrieve("audio_features", pid)
        assert result is None

    def test_retrieve_returns_exact_vector_values(self, qdrant_repo):
        pid = _uid()
        v = [float(i) / 100 for i in range(_DIM)]
        norm = sum(x**2 for x in v) ** 0.5
        v_normed = [x / norm for x in v]
        qdrant_repo.upsert("audio_features", pid, v_normed, {})

        result = qdrant_repo.retrieve("audio_features", pid)
        for got, want in zip(result, v_normed):
            assert abs(got - want) < 1e-5


# ===========================================================================
# TestRecommendationsEndpoint — FastAPI integration
# ===========================================================================


class TestRecommendationsEndpoint:
    """Integration tests for GET /api/v1/recommendations."""

    @pytest_asyncio.fixture
    async def rec_fixtures(self, client, app_db):
        from karaoke_shared.models.track import TrackCreate
        from karaoke_shared.repositories import SQLiteRepository
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        from app.main import app

        repo = SQLiteRepository(app_db)

        track = await repo.create_track(
            TrackCreate(
                artist="Queen",
                title="Bohemian Rhapsody",
                source="catalog",
                status="ready",
                duration_sec=354,
            )
        )

        r = await client.post("/api/v1/sessions", json={"room_id": "room-rec-1"})
        assert r.status_code == 201
        session_id = r.json()["id"]

        qdrant_client = QdrantClient(":memory:")
        for coll, dim in [("audio_features", 45), ("lyrics_embeddings", 384)]:
            qdrant_client.create_collection(
                collection_name=coll,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

        app.state.qdrant = qdrant_client

        yield {
            "session_id": session_id,
            "track_id": track.id,
            "repo": repo,
        }

        app.state.qdrant = None

    async def test_returns_200(self, client, rec_fixtures):
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={"session_id": f["session_id"], "limit": 5},
        )
        assert r.status_code == 200

    async def test_response_has_strategy_and_tracks(self, client, rec_fixtures):
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={"session_id": f["session_id"]},
        )
        body = r.json()
        assert "strategy" in body
        assert "tracks" in body
        assert isinstance(body["tracks"], list)

    async def test_returns_popular_strategy(self, client, rec_fixtures):
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={"session_id": f["session_id"]},
        )
        assert r.json()["strategy"] == "popular"

    async def test_track_items_have_required_fields(self, client, rec_fixtures):
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={"session_id": f["session_id"]},
        )
        for item in r.json()["tracks"]:
            assert "id" in item
            assert "artist" in item
            assert "title" in item
            assert "duration_sec" in item
            assert "similarity_score" in item

    async def test_limit_parameter_respected(self, client, app_db, rec_fixtures):
        from karaoke_shared.models.track import TrackCreate
        from karaoke_shared.repositories import SQLiteRepository

        repo = SQLiteRepository(app_db)
        for i in range(5):
            await repo.create_track(
                TrackCreate(
                    artist=f"Artist {i}",
                    title=f"Song {i}",
                    source="catalog",
                    status="ready",
                )
            )

        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={"session_id": f["session_id"], "limit": 2},
        )
        assert r.status_code == 200
        assert len(r.json()["tracks"]) <= 2

    async def test_missing_session_id_returns_422(self, client, rec_fixtures):
        r = await client.get("/api/v1/recommendations", params={})
        assert r.status_code == 422

    async def test_strategy_is_valid_enum(self, client, rec_fixtures):
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={"session_id": f["session_id"]},
        )
        assert r.json()["strategy"] in {"popular"}


# ===========================================================================
# TestQueueServiceFinishPlaying
# ===========================================================================


class TestQueueServiceFinishPlaying:
    """Integration tests for QueueService.finish_playing (no recommendation updates)."""

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
        result = await service.finish_playing("nonexistent")

        assert result is None

    async def test_increments_play_count(self):
        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_service(entry)

        await service.finish_playing("e-1")

        repo.increment_play_count.assert_called_once_with("t-1")

    async def test_increments_tracks_played(self):
        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_service(entry)

        await service.finish_playing("e-1")

        repo.increment_tracks_played.assert_called_once_with("p-1")

    async def test_creates_play_history(self):
        from karaoke_shared.models.play_history import PlayHistoryCreate

        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_service(entry)

        await service.finish_playing("e-1")

        repo.create_play_history.assert_called_once()
        call_arg: PlayHistoryCreate = repo.create_play_history.call_args.args[0]
        assert call_arg.session_id == "s-1"
        assert call_arg.participant_id == "p-1"
        assert call_arg.track_id == "t-1"

    async def test_marks_entry_as_done(self):
        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_service(entry)

        await service.finish_playing("e-1")

        repo.update_queue_entry_status.assert_called_with("e-1", "done")
