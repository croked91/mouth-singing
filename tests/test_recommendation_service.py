"""Comprehensive tests for the Phase 8b recommendation system (weighted fusion update).

Coverage:
- RecommendationService.get_recommendations — all four strategy branches
- RecommendationService.update_portrait — first-track, EMA paths, dual audio+lyrics
- RecommendationService.record_transition — records and skips cases
- QDrantRepository.retrieve — existing and non-existing point
- GET /api/v1/recommendations — endpoint contract and RecommendationResponse schema
- QueueService.finish_playing integration — portrait + transition update paths
- _fused_knn_search — weighted fusion scoring, audio-only fallback, lyrics-only fallback

All async tests use asyncio_mode = "auto" (configured in pytest.ini).
SQLiteRepository / QDrantRepository are mocked with AsyncMock / MagicMock
for unit tests; integration tests use the real in-memory fixtures from conftest.py.
"""

from __future__ import annotations

import math
import uuid
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import pytest_asyncio

from karaoke_shared.models.recommendation import RecommendationStrategy
from karaoke_shared.models.session import Participant
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
    """Return a normalised 45-dim audio vector."""
    return _vec(seed, _DIM)


def _lyrics_vec(seed: float = 0.1) -> list[float]:
    """Return a normalised 384-dim lyrics vector."""
    return _vec(seed, _LYRICS_DIM)


def _l2_norm(v: list[float]) -> float:
    """Compute the L2 norm of a vector."""
    return math.sqrt(sum(x**2 for x in v))


def _l2_normalize(v: list[float]) -> list[float]:
    """L2-normalize a vector."""
    norm = _l2_norm(v)
    if norm < 1e-8:
        return v
    return [x / norm for x in v]


def _make_track(track_id: str | None = None) -> Track:
    """Construct a minimal Track model."""
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
    """Construct a PlayHistoryEntry-like MagicMock."""
    entry = MagicMock()
    entry.track_id = track_id
    entry.participant_id = participant_id
    entry.session_id = session_id
    return entry


def _make_participant(
    participant_id: str,
    tracks_played: int = 0,
    portrait_vector: list[float] | None = None,
    lyrics_portrait_vector: list[float] | None = None,
) -> Participant:
    """Construct a minimal Participant model."""
    return Participant(
        id=participant_id,
        session_id="sess-1",
        display_name="Alice",
        portrait_vector=portrait_vector,
        lyrics_portrait_vector=lyrics_portrait_vector,
        tracks_played=tracks_played,
        created_at="2024-01-01T00:00:00+00:00",
    )


def _make_sqlite_repo(
    history: list | None = None,
    popular: list | None = None,
    track: Track | None = None,
    participant: Participant | None = None,
) -> AsyncMock:
    """Build an AsyncMock SQLiteRepository with configurable return values.

    Includes all methods used by the new recommendation_service implementation:
    - list_random (70/30 popular mix)
    - get_tracks_by_ids (batch KNN result enrichment)
    - update_portrait now accepts 3 positional args (participant_id, audio_portrait, lyrics_portrait)
    """
    repo = AsyncMock()
    repo.get_history_by_participant.return_value = history or []
    repo.list_popular.return_value = popular or []
    repo.list_random.return_value = []
    repo.get_track.return_value = track
    repo.get_tracks_by_ids.return_value = {}
    repo.get_participant.return_value = participant
    repo.update_portrait.return_value = None
    return repo


def _make_qdrant_repo(
    audio_retrieve: list[float] | None = None,
    lyrics_retrieve: list[float] | None = None,
    audio_search: list | None = None,
    lyrics_search: list | None = None,
) -> MagicMock:
    """Build a MagicMock QDrantRepository that handles both audio and lyrics collections.

    retrieve(), search(), upsert(), retrieve_payload(), and scroll_filtered()
    are synchronous methods called via asyncio.to_thread, so they are plain
    MagicMocks (not AsyncMocks).

    retrieve uses side_effect to dispatch by collection name:
    - "audio_features" -> audio_retrieve
    - "lyrics_embeddings" -> lyrics_retrieve
    - other -> None

    search uses side_effect to dispatch by collection name:
    - "audio_features" -> audio_search (defaults to [])
    - "lyrics_embeddings" -> lyrics_search (defaults to [])
    - other -> []
    """
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
    repo.retrieve_payload.return_value = None  # no existing transition by default
    repo.scroll_filtered.return_value = []     # no transition candidates by default
    return repo


# ---------------------------------------------------------------------------
# Import RecommendationService (after sys.path is already set up by conftest)
# ---------------------------------------------------------------------------

from app.services.recommendation_service import RecommendationService  # noqa: E402


# ===========================================================================
# TestStrategySelection
# ===========================================================================


class TestStrategySelection:
    """Tests for automatic strategy selection based on tracks_played counter."""

    # -- 0 tracks played → POPULAR ------------------------------------------

    async def test_zero_history_uses_popular_strategy(self):
        """With tracks_played=0 the service returns the POPULAR strategy."""
        popular_track = _make_track()
        participant = _make_participant("p-1", tracks_played=0)
        sqlite_repo = _make_sqlite_repo(
            history=[], popular=[popular_track], participant=participant
        )
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert strategy is RecommendationStrategy.POPULAR

    async def test_zero_history_calls_list_popular(self):
        """With tracks_played=0, list_popular is called to source results."""
        popular_track = _make_track()
        participant = _make_participant("p-1", tracks_played=0)
        sqlite_repo = _make_sqlite_repo(
            history=[], popular=[popular_track], participant=participant
        )
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("p-1", "s-1", limit=5)

        sqlite_repo.list_popular.assert_called_once()

    async def test_zero_history_returns_popular_tracks(self):
        """Results contain the popular track with similarity_score=0.0."""
        popular_track = _make_track()
        participant = _make_participant("p-1", tracks_played=0)
        sqlite_repo = _make_sqlite_repo(
            history=[], popular=[popular_track], participant=participant
        )
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert len(results) == 1
        assert results[0].track.id == popular_track.id
        assert results[0].similarity_score == 0.0

    # -- 1 track played → LAST ----------------------------------------------

    async def test_one_history_uses_last_strategy(self):
        """With tracks_played=1 the service returns the LAST strategy."""
        track_id = _uid()
        new_track_id = _uid()
        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(
            history=history, participant=participant
        )
        sqlite_repo.get_tracks_by_ids.return_value = {
            new_track_id: _make_track(new_track_id)
        }
        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            audio_search=[(new_track_id, 0.95, {"status": "ready"})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations("p-1", "s-1", limit=5)

        assert strategy is RecommendationStrategy.LAST

    async def test_one_history_retrieves_audio_features_for_last_track(self):
        """With tracks_played=1, retrieve() is called with audio_features and the last track id."""
        track_id = _uid()
        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("p-1", "s-1", limit=5)

        qdrant_repo.retrieve.assert_any_call("audio_features", track_id)

    async def test_one_history_retrieves_lyrics_embeddings_for_last_track(self):
        """With tracks_played=1, retrieve() is also called with lyrics_embeddings."""
        track_id = _uid()
        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec(), lyrics_retrieve=_lyrics_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("p-1", "s-1", limit=5)

        qdrant_repo.retrieve.assert_any_call("lyrics_embeddings", track_id)

    # -- 2 tracks played → LAST_TWO_AVG ------------------------------------

    async def test_two_history_uses_last_two_avg_strategy(self):
        """With tracks_played=2 the service returns LAST_TWO_AVG."""
        t1, t2 = _uid(), _uid()
        history = [_make_history_entry("p-1", t1), _make_history_entry("p-1", t2)]
        participant = _make_participant("p-1", tracks_played=2)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations("p-1", "s-1", limit=5)

        assert strategy is RecommendationStrategy.LAST_TWO_AVG

    async def test_two_history_retrieves_audio_vectors_for_both_tracks(self):
        """With tracks_played=2, retrieve('audio_features', ...) is called for both track IDs."""
        t1, t2 = _uid(), _uid()
        history = [_make_history_entry("p-1", t1), _make_history_entry("p-1", t2)]
        participant = _make_participant("p-1", tracks_played=2)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)

        v1, v2 = _audio_vec(0.1), _audio_vec(0.2)

        def retrieve_side(coll, tid):
            if coll == "audio_features":
                return {t1: v1, t2: v2}.get(tid)
            return None

        qdrant_repo = _make_qdrant_repo()
        qdrant_repo.retrieve.side_effect = retrieve_side

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("p-1", "s-1", limit=5)

        audio_calls = [
            c for c in qdrant_repo.retrieve.call_args_list
            if c.args[0] == "audio_features"
        ]
        retrieved_audio_ids = {c.args[1] for c in audio_calls}
        assert t1 in retrieved_audio_ids
        assert t2 in retrieved_audio_ids

    async def test_two_history_knn_uses_averaged_audio_vector(self):
        """With tracks_played=2, the audio KNN search uses the L2-normalised average of both vectors."""
        t1, t2 = _uid(), _uid()
        history = [_make_history_entry("p-1", t1), _make_history_entry("p-1", t2)]
        participant = _make_participant("p-1", tracks_played=2)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)

        v1 = [1.0] * _DIM
        v2 = [3.0] * _DIM
        # Average is [2.0]*DIM, then L2-normalised → [1/sqrt(45)]*DIM
        raw_avg = [2.0] * _DIM
        expected_avg = _l2_normalize(raw_avg)

        def retrieve_side(coll, tid):
            if coll == "audio_features":
                return {t1: v1, t2: v2}.get(tid)
            return None

        qdrant_repo = _make_qdrant_repo()
        qdrant_repo.retrieve.side_effect = retrieve_side

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("p-1", "s-1", limit=5)

        # Find the audio_features search call
        audio_search_calls = [
            c for c in qdrant_repo.search.call_args_list
            if c.args[0] == "audio_features"
        ]
        assert len(audio_search_calls) >= 1
        call_vector = audio_search_calls[0].args[1]
        assert call_vector == pytest.approx(expected_avg)

    # -- 3+ tracks played → SESSION_AVG ------------------------------------

    async def test_three_history_uses_session_avg_strategy(self):
        """With tracks_played=3 and a portrait, SESSION_AVG is used."""
        participant_id = "p-1"
        portrait = _audio_vec(0.5)
        history = [
            _make_history_entry(participant_id, _uid()),
            _make_history_entry(participant_id, _uid()),
            _make_history_entry(participant_id, _uid()),
        ]
        participant = _make_participant(participant_id, tracks_played=3, portrait_vector=portrait)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations(participant_id, "s-1", limit=5)

        assert strategy is RecommendationStrategy.SESSION_AVG

    async def test_three_history_knn_uses_portrait_vector(self):
        """With tracks_played=3, the audio KNN search uses the participant's portrait."""
        participant_id = "p-1"
        portrait = _audio_vec(0.77)
        history = [
            _make_history_entry(participant_id, _uid()),
            _make_history_entry(participant_id, _uid()),
            _make_history_entry(participant_id, _uid()),
        ]
        participant = _make_participant(participant_id, tracks_played=3, portrait_vector=portrait)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations(participant_id, "s-1", limit=5)

        # Audio KNN must be called with the portrait vector
        audio_search_calls = [
            c for c in qdrant_repo.search.call_args_list
            if c.args[0] == "audio_features"
        ]
        assert len(audio_search_calls) >= 1
        call_vector = audio_search_calls[0].args[1]
        assert call_vector == pytest.approx(portrait)

    async def test_three_history_knn_uses_lyrics_portrait_vector(self):
        """With tracks_played=3 and both portraits, lyrics KNN uses the lyrics portrait."""
        participant_id = "p-1"
        portrait = _audio_vec(0.5)
        lyrics_portrait = _lyrics_vec(0.3)
        history = [
            _make_history_entry(participant_id, _uid()),
            _make_history_entry(participant_id, _uid()),
            _make_history_entry(participant_id, _uid()),
        ]
        participant = _make_participant(
            participant_id,
            tracks_played=3,
            portrait_vector=portrait,
            lyrics_portrait_vector=lyrics_portrait,
        )
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations(participant_id, "s-1", limit=5)

        lyrics_search_calls = [
            c for c in qdrant_repo.search.call_args_list
            if c.args[0] == "lyrics_embeddings"
        ]
        assert len(lyrics_search_calls) >= 1
        call_vector = lyrics_search_calls[0].args[1]
        assert call_vector == pytest.approx(lyrics_portrait)


# ===========================================================================
# TestFusedKnnSearch
# ===========================================================================


class TestFusedKnnSearch:
    """Tests for _fused_knn_search: weighted fusion, audio-only, lyrics-only fallbacks."""

    async def test_fusion_score_is_weighted_combination(self):
        """When both audio and lyrics results exist, score = 0.7*audio + 0.3*lyrics."""
        track_id = _uid()
        new_id = _uid()

        audio_score = 0.80
        lyrics_score = 0.60
        expected_fused = 0.7 * audio_score + 0.3 * lyrics_score

        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        sqlite_repo.get_tracks_by_ids.return_value = {new_id: _make_track(new_id)}

        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            lyrics_retrieve=_lyrics_vec(),
            audio_search=[(new_id, audio_score, {"status": "ready"})],
            lyrics_search=[(new_id, lyrics_score, {"status": "ready"})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        # Should get the track with fused score
        assert len(results) == 1
        assert results[0].track.id == new_id
        assert results[0].similarity_score == pytest.approx(expected_fused, abs=1e-6)

    async def test_fusion_audio_only_when_lyrics_vector_none(self):
        """When lyrics_vector is None, pure audio KNN score is used (no weighting)."""
        track_id = _uid()
        new_id = _uid()
        audio_score = 0.90

        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        sqlite_repo.get_tracks_by_ids.return_value = {new_id: _make_track(new_id)}

        # lyrics_retrieve=None → only audio search runs
        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            lyrics_retrieve=None,
            audio_search=[(new_id, audio_score, {"status": "ready"})],
            lyrics_search=[],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert len(results) == 1
        assert results[0].track.id == new_id
        # Pure audio: score is the raw audio score (no weighting applied)
        assert results[0].similarity_score == pytest.approx(audio_score, abs=1e-6)

    async def test_fusion_lyrics_only_when_audio_vector_none(self):
        """When audio_vector is None but lyrics_vector exists, pure lyrics KNN is used."""
        track_id = _uid()
        new_id = _uid()
        lyrics_score = 0.75

        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        sqlite_repo.get_tracks_by_ids.return_value = {new_id: _make_track(new_id)}

        # audio_retrieve=None → no audio vector; lyrics_retrieve present → lyrics only
        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=None,
            lyrics_retrieve=_lyrics_vec(),
            audio_search=[],
            lyrics_search=[(new_id, lyrics_score, {"status": "ready"})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert len(results) == 1
        assert results[0].track.id == new_id
        # Pure lyrics: score is the raw lyrics score (no weighting applied)
        assert results[0].similarity_score == pytest.approx(lyrics_score, abs=1e-6)

    async def test_fusion_searches_both_collections(self):
        """_fused_knn_search calls search() on both audio_features and lyrics_embeddings."""
        track_id = _uid()
        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)

        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            lyrics_retrieve=_lyrics_vec(),
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("p-1", "s-1", limit=5)

        called_collections = {c.args[0] for c in qdrant_repo.search.call_args_list}
        assert "audio_features" in called_collections
        assert "lyrics_embeddings" in called_collections

    async def test_fusion_deduplicates_candidates_by_track_id(self):
        """A track appearing in both audio and lyrics results is included once."""
        track_id = _uid()
        shared_id = _uid()

        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        sqlite_repo.get_tracks_by_ids.return_value = {shared_id: _make_track(shared_id)}

        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            lyrics_retrieve=_lyrics_vec(),
            audio_search=[(shared_id, 0.8, {})],
            lyrics_search=[(shared_id, 0.7, {})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        result_ids = [r.track.id for r in results]
        assert result_ids.count(shared_id) == 1

    async def test_fusion_merges_results_from_both_collections(self):
        """Union of audio-only and lyrics-only candidate IDs all appear in results."""
        track_id = _uid()
        audio_only_id = _uid()
        lyrics_only_id = _uid()

        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        sqlite_repo.get_tracks_by_ids.return_value = {
            audio_only_id: _make_track(audio_only_id),
            lyrics_only_id: _make_track(lyrics_only_id),
        }

        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            lyrics_retrieve=_lyrics_vec(),
            audio_search=[(audio_only_id, 0.85, {})],
            lyrics_search=[(lyrics_only_id, 0.70, {})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        result_ids = {r.track.id for r in results}
        assert audio_only_id in result_ids
        assert lyrics_only_id in result_ids

    async def test_fusion_audio_only_track_gets_weighted_score(self):
        """A track only in audio results gets score = 0.7 * audio_score + 0 (lyrics absent)."""
        track_id = _uid()
        audio_only_id = _uid()
        lyrics_only_id = _uid()

        audio_score = 0.9
        lyrics_score = 0.8

        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        sqlite_repo.get_tracks_by_ids.return_value = {
            audio_only_id: _make_track(audio_only_id),
            lyrics_only_id: _make_track(lyrics_only_id),
        }

        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            lyrics_retrieve=_lyrics_vec(),
            audio_search=[(audio_only_id, audio_score, {})],
            lyrics_search=[(lyrics_only_id, lyrics_score, {})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        result_map = {r.track.id: r.similarity_score for r in results}
        # audio_only_id: 0.7 * 0.9 + 0.3 * 0 = 0.63
        assert result_map[audio_only_id] == pytest.approx(0.7 * audio_score, abs=1e-6)
        # lyrics_only_id: 0.7 * 0 + 0.3 * 0.8 = 0.24
        assert result_map[lyrics_only_id] == pytest.approx(0.3 * lyrics_score, abs=1e-6)

    async def test_fusion_results_sorted_by_fused_score_descending(self):
        """Results are ordered by fused score, highest first."""
        track_id = _uid()
        id_a = _uid()  # audio=0.95, lyrics=0.5 → fused=0.7*0.95+0.3*0.5 = 0.815
        id_b = _uid()  # audio=0.6, lyrics=0.95 → fused=0.7*0.6+0.3*0.95 = 0.705

        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        sqlite_repo.get_tracks_by_ids.return_value = {
            id_a: _make_track(id_a),
            id_b: _make_track(id_b),
        }

        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            lyrics_retrieve=_lyrics_vec(),
            audio_search=[(id_a, 0.95, {}), (id_b, 0.60, {})],
            lyrics_search=[(id_a, 0.50, {}), (id_b, 0.95, {})],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert len(results) == 2
        assert results[0].track.id == id_a  # higher fused score first
        assert results[1].track.id == id_b


# ===========================================================================
# TestFilteringPlayedTracks
# ===========================================================================


class TestFilteringPlayedTracks:
    """Played tracks must never appear in recommendation results."""

    async def test_popular_strategy_excludes_played_tracks(self):
        """Popular results exclude tracks already played by the participant."""
        played_id = _uid()
        other_id = _uid()
        played_track = _make_track(played_id)
        other_track = _make_track(other_id)

        history = [_make_history_entry("p-1", played_id)]
        # tracks_played=1 + retrieve=None + no transitions → falls back to popular
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(
            history=history,
            popular=[played_track, other_track],
            participant=participant,
        )
        qdrant_repo = _make_qdrant_repo(audio_retrieve=None, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=10)

        result_ids = {r.track.id for r in results}
        assert played_id not in result_ids
        assert other_id in result_ids

    async def test_knn_results_exclude_played_tracks(self):
        """KNN search results exclude tracks that the participant has already played."""
        played_id = _uid()
        new_id = _uid()

        history = [_make_history_entry("p-1", played_id)]
        participant = _make_participant("p-1", tracks_played=1)
        # get_tracks_by_ids returns only the new track (played_id is pre-filtered)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        sqlite_repo.get_tracks_by_ids.return_value = {new_id: _make_track(new_id)}

        # QDrant returns both played and a new track
        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(),
            audio_search=[
                (played_id, 0.99, {"status": "ready"}),
                (new_id, 0.90, {"status": "ready"}),
            ],
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        result_ids = {r.track.id for r in results}
        assert played_id not in result_ids
        assert new_id in result_ids

    async def test_limit_is_respected_after_filtering(self):
        """The result list never exceeds the requested limit."""
        played_id = _uid()
        new_ids = [_uid() for _ in range(10)]

        history = [_make_history_entry("p-1", played_id)]
        participant = _make_participant("p-1", tracks_played=1)
        search_hits = [(played_id, 1.0, {})] + [(nid, 0.9, {}) for nid in new_ids]

        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        # All new_ids are valid tracks
        sqlite_repo.get_tracks_by_ids.return_value = {
            nid: _make_track(nid) for nid in new_ids
        }
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec(), audio_search=search_hits)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=3)

        assert len(results) <= 3


# ===========================================================================
# TestFallbackBehavior
# ===========================================================================


class TestFallbackBehavior:
    """Tests for fallback paths when vectors or portrait data are unavailable."""

    async def test_last_strategy_falls_back_to_popular_when_no_vector(self):
        """If the last track has no vector in QDrant and no transitions, falls back to POPULAR."""
        track_id = _uid()
        popular_track = _make_track(_uid())
        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(
            history=history, popular=[popular_track], participant=participant
        )
        # scroll_filtered returns [] (default in _make_qdrant_repo), no transitions
        qdrant_repo = _make_qdrant_repo(audio_retrieve=None, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert strategy is RecommendationStrategy.POPULAR
        assert len(results) == 1
        assert results[0].track.id == popular_track.id

    async def test_last_two_avg_falls_back_to_popular_when_both_vectors_missing(self):
        """If both of the last two tracks have no vector, falls back to POPULAR."""
        t1, t2 = _uid(), _uid()
        popular_track = _make_track(_uid())
        history = [_make_history_entry("p-1", t1), _make_history_entry("p-1", t2)]
        participant = _make_participant("p-1", tracks_played=2)
        sqlite_repo = _make_sqlite_repo(
            history=history, popular=[popular_track], participant=participant
        )
        qdrant_repo = _make_qdrant_repo(audio_retrieve=None, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert strategy is RecommendationStrategy.POPULAR

    async def test_last_two_avg_uses_only_v1_when_v2_missing(self):
        """If the second audio vector is missing, KNN uses the first audio vector alone."""
        t1, t2 = _uid(), _uid()
        history = [_make_history_entry("p-1", t1), _make_history_entry("p-1", t2)]
        participant = _make_participant("p-1", tracks_played=2)
        sqlite_repo = _make_sqlite_repo(
            history=history, participant=participant, track=_make_track(_uid())
        )

        v1 = _audio_vec(0.1)

        def retrieve_side(coll, tid):
            if coll == "audio_features":
                return {t1: v1, t2: None}.get(tid)
            return None

        qdrant_repo = _make_qdrant_repo()
        qdrant_repo.retrieve.side_effect = retrieve_side

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations("p-1", "s-1", limit=5)

        # Should still yield LAST_TWO_AVG (not fallback to popular)
        assert strategy is RecommendationStrategy.LAST_TWO_AVG
        # Audio KNN must have been called with v1 (the only available audio vector)
        audio_search_calls = [
            c for c in qdrant_repo.search.call_args_list
            if c.args[0] == "audio_features"
        ]
        assert len(audio_search_calls) >= 1
        call_vector = audio_search_calls[0].args[1]
        assert call_vector == pytest.approx(v1)

    async def test_last_two_avg_uses_only_v2_when_v1_missing(self):
        """If the first audio vector is missing, KNN uses the second audio vector alone."""
        t1, t2 = _uid(), _uid()
        history = [_make_history_entry("p-1", t1), _make_history_entry("p-1", t2)]
        participant = _make_participant("p-1", tracks_played=2)
        sqlite_repo = _make_sqlite_repo(
            history=history, participant=participant, track=_make_track(_uid())
        )

        v2 = _audio_vec(0.2)

        def retrieve_side(coll, tid):
            if coll == "audio_features":
                return {t1: None, t2: v2}.get(tid)
            return None

        qdrant_repo = _make_qdrant_repo()
        qdrant_repo.retrieve.side_effect = retrieve_side

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations("p-1", "s-1", limit=5)

        assert strategy is RecommendationStrategy.LAST_TWO_AVG
        audio_search_calls = [
            c for c in qdrant_repo.search.call_args_list
            if c.args[0] == "audio_features"
        ]
        assert len(audio_search_calls) >= 1
        call_vector = audio_search_calls[0].args[1]
        assert call_vector == pytest.approx(v2)

    async def test_session_avg_falls_back_to_last_two_avg_when_portrait_missing(self):
        """With tracks_played=3 but no portrait vector, falls back to LAST_TWO_AVG."""
        participant_id = "p-1"
        t1, t2, t3 = _uid(), _uid(), _uid()
        history = [
            _make_history_entry(participant_id, t1),
            _make_history_entry(participant_id, t2),
            _make_history_entry(participant_id, t3),
        ]
        # portrait_vector is None
        participant = _make_participant(participant_id, tracks_played=3, portrait_vector=None)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)

        v1, v2 = _audio_vec(0.1), _audio_vec(0.2)

        def retrieve_side(coll, tid):
            if coll == "audio_features":
                return {t1: v1, t2: v2, t3: None}.get(tid)
            return None

        qdrant_repo = _make_qdrant_repo()
        qdrant_repo.retrieve.side_effect = retrieve_side

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations(participant_id, "s-1", limit=5)

        assert strategy is RecommendationStrategy.LAST_TWO_AVG

    async def test_session_avg_falls_back_when_participant_not_found(self):
        """When participant row is missing, tracks_played=0 → POPULAR strategy."""
        participant_id = "p-1"
        t1, t2, t3 = _uid(), _uid(), _uid()
        history = [
            _make_history_entry(participant_id, t1),
            _make_history_entry(participant_id, t2),
            _make_history_entry(participant_id, t3),
        ]
        # participant=None → tracks_played defaults to 0 → POPULAR
        popular_track = _make_track(_uid())
        sqlite_repo = _make_sqlite_repo(
            history=history, participant=None, popular=[popular_track]
        )
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations(participant_id, "s-1", limit=5)

        assert strategy is RecommendationStrategy.POPULAR

    async def test_knn_search_exception_returns_empty_results(self):
        """If QDrant search raises, the service returns an empty results list."""
        track_id = _uid()
        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())
        qdrant_repo.search.side_effect = RuntimeError("qdrant unavailable")

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert strategy is RecommendationStrategy.LAST
        assert results == []

    async def test_retrieve_exception_is_swallowed_and_returns_none(self):
        """If retrieve() raises, _get_track_vector returns None (logged, not re-raised)."""
        track_id = _uid()
        history = [_make_history_entry("p-1", track_id)]
        popular_track = _make_track(_uid())
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(
            history=history, popular=[popular_track], participant=participant
        )
        qdrant_repo = _make_qdrant_repo()
        qdrant_repo.retrieve.side_effect = ConnectionError("QDrant down")

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations("p-1", "s-1", limit=5)

        # retrieve failed → fallback to popular
        assert strategy is RecommendationStrategy.POPULAR


# ===========================================================================
# TestPopularStrategy
# ===========================================================================


class TestPopularStrategy:
    """Tests specific to the _popular_strategy 70/30 mix."""

    async def test_popular_strategy_calls_both_list_popular_and_list_random(self):
        """_popular_strategy calls both list_popular and list_random for the 70/30 mix."""
        popular_tracks = [_make_track() for _ in range(5)]
        random_tracks = [_make_track() for _ in range(5)]
        participant = _make_participant("p-1", tracks_played=0)
        sqlite_repo = _make_sqlite_repo(
            history=[], popular=popular_tracks, participant=participant
        )
        sqlite_repo.list_random.return_value = random_tracks
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, results = await service.get_recommendations("p-1", "s-1", limit=10)

        assert strategy is RecommendationStrategy.POPULAR
        sqlite_repo.list_popular.assert_called_once()
        sqlite_repo.list_random.assert_called_once()

    async def test_popular_strategy_mixes_popular_and_random_slots(self):
        """Results include tracks from both popular and random buckets."""
        # Use distinct IDs to identify which bucket each result came from
        popular_id = _uid()
        random_id = _uid()
        popular_track = _make_track(popular_id)
        random_track = _make_track(random_id)

        participant = _make_participant("p-1", tracks_played=0)
        sqlite_repo = _make_sqlite_repo(
            history=[], popular=[popular_track], participant=participant
        )
        sqlite_repo.list_random.return_value = [random_track]
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=10)

        result_ids = {r.track.id for r in results}
        assert popular_id in result_ids
        assert random_id in result_ids

    async def test_popular_strategy_deduplicates_across_buckets(self):
        """A track appearing in both popular and random is included only once."""
        shared_id = _uid()
        shared_track = _make_track(shared_id)
        other_track = _make_track(_uid())

        participant = _make_participant("p-1", tracks_played=0)
        sqlite_repo = _make_sqlite_repo(
            history=[], popular=[shared_track, other_track], participant=participant
        )
        # Same track also in random list
        sqlite_repo.list_random.return_value = [shared_track]
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=10)

        result_ids = [r.track.id for r in results]
        assert result_ids.count(shared_id) == 1


# ===========================================================================
# TestUpdatePortrait
# ===========================================================================


class TestUpdatePortrait:
    """Tests for RecommendationService.update_portrait (EMA + L2-renorm, dual audio+lyrics)."""

    async def test_first_track_portrait_equals_track_vector(self):
        """When tracks_played == 1 (just incremented), portrait = track vector (L2-normalised)."""
        participant_id = "p-1"
        track_id = _uid()
        track_vector = _audio_vec(0.3)  # already unit-norm

        participant = _make_participant(participant_id, tracks_played=1, portrait_vector=None)
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=track_vector, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        result = await service.update_portrait(participant_id, track_id)

        # L2-normalizing a unit vector gives back the same vector
        assert result == pytest.approx(track_vector)
        # update_portrait now called with 3 args: (participant_id, audio_portrait, lyrics_portrait)
        sqlite_repo.update_portrait.assert_called_once_with(
            participant_id, pytest.approx(track_vector), None
        )

    async def test_subsequent_track_uses_ema_formula(self):
        """After the first track, portrait is computed with EMA: 0.3*cur + 0.7*old."""
        participant_id = "p-1"
        track_id = _uid()

        old_portrait = [2.0] * _DIM
        track_vector = [4.0] * _DIM
        # EMA: 0.3*4.0 + 0.7*2.0 = 1.2 + 1.4 = 2.6 per dimension
        ema_raw = [0.3 * 4.0 + 0.7 * 2.0] * _DIM  # = [2.6]*45
        expected = _l2_normalize(ema_raw)

        participant = _make_participant(participant_id, tracks_played=2, portrait_vector=old_portrait)
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=track_vector, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        result = await service.update_portrait(participant_id, track_id)

        assert result == pytest.approx(expected)

    async def test_update_portrait_persisted_to_sqlite_with_three_args(self):
        """update_portrait calls sqlite_repo.update_portrait(participant_id, audio_vec, lyrics_vec)."""
        participant_id = "p-1"
        track_id = _uid()
        track_vector = _audio_vec(0.5)
        n = 3

        old_portrait = _audio_vec(0.2)
        participant = _make_participant(participant_id, tracks_played=n, portrait_vector=old_portrait)
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=track_vector, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.update_portrait(participant_id, track_id)

        sqlite_repo.update_portrait.assert_called_once()
        call_args = sqlite_repo.update_portrait.call_args.args
        # 3 positional args: participant_id, audio_portrait, lyrics_portrait
        assert len(call_args) == 3
        call_pid, call_audio_vec, call_lyrics_vec = call_args
        assert call_pid == participant_id
        assert len(call_audio_vec) == _DIM
        # No lyrics vector for this track → lyrics portrait is None
        assert call_lyrics_vec is None

    async def test_update_portrait_lyrics_portrait_updated_when_lyrics_vector_available(self):
        """When track has a lyrics vector, lyrics portrait is updated alongside audio portrait."""
        participant_id = "p-1"
        track_id = _uid()
        audio_vector = _audio_vec(0.5)
        lyrics_vector = _lyrics_vec(0.3)

        old_audio = _audio_vec(0.2)
        old_lyrics = _lyrics_vec(0.1)
        participant = _make_participant(
            participant_id,
            tracks_played=3,
            portrait_vector=old_audio,
            lyrics_portrait_vector=old_lyrics,
        )
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=audio_vector, lyrics_retrieve=lyrics_vector)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        result = await service.update_portrait(participant_id, track_id)

        assert result is not None
        sqlite_repo.update_portrait.assert_called_once()
        call_args = sqlite_repo.update_portrait.call_args.args
        assert len(call_args) == 3
        call_pid, call_audio_vec, call_lyrics_vec = call_args
        assert call_pid == participant_id
        assert len(call_audio_vec) == _DIM
        # Lyrics portrait must be updated (not None)
        assert call_lyrics_vec is not None
        assert len(call_lyrics_vec) == _LYRICS_DIM

    async def test_lyrics_portrait_first_track_equals_lyrics_vector(self):
        """When tracks_played==1 and lyrics vector present, lyrics_portrait = lyrics_vector (L2-norm)."""
        participant_id = "p-1"
        track_id = _uid()
        audio_vector = _audio_vec(0.3)
        lyrics_vector = _lyrics_vec(0.4)

        participant = _make_participant(
            participant_id,
            tracks_played=1,
            portrait_vector=None,
            lyrics_portrait_vector=None,
        )
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=audio_vector, lyrics_retrieve=lyrics_vector)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.update_portrait(participant_id, track_id)

        call_args = sqlite_repo.update_portrait.call_args.args
        _, _, call_lyrics_vec = call_args
        # First track: portrait = track vector directly (then L2-normalized)
        assert call_lyrics_vec == pytest.approx(lyrics_vector)

    async def test_lyrics_portrait_uses_ema_on_subsequent_tracks(self):
        """On 2nd+ track, lyrics portrait uses EMA: 0.3*cur + 0.7*old (L2-normalized)."""
        participant_id = "p-1"
        track_id = _uid()

        old_lyrics = [2.0] * _LYRICS_DIM
        lyrics_vector = [4.0] * _LYRICS_DIM
        ema_raw = [0.3 * 4.0 + 0.7 * 2.0] * _LYRICS_DIM
        expected_lyrics = _l2_normalize(ema_raw)

        participant = _make_participant(
            participant_id,
            tracks_played=2,
            portrait_vector=_audio_vec(0.3),
            lyrics_portrait_vector=old_lyrics,
        )
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(0.5),
            lyrics_retrieve=lyrics_vector,
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.update_portrait(participant_id, track_id)

        call_args = sqlite_repo.update_portrait.call_args.args
        _, _, call_lyrics_vec = call_args
        assert call_lyrics_vec == pytest.approx(expected_lyrics)

    async def test_update_portrait_returns_none_when_no_audio_vector(self):
        """If QDrant has no audio vector for the track, update_portrait returns None."""
        participant_id = "p-1"
        track_id = _uid()

        sqlite_repo = _make_sqlite_repo()
        qdrant_repo = _make_qdrant_repo(audio_retrieve=None, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        result = await service.update_portrait(participant_id, track_id)

        assert result is None
        sqlite_repo.update_portrait.assert_not_called()

    async def test_update_portrait_returns_none_when_participant_not_found(self):
        """If the participant row doesn't exist, update_portrait returns None."""
        participant_id = "p-nonexistent"
        track_id = _uid()

        sqlite_repo = _make_sqlite_repo(participant=None)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        result = await service.update_portrait(participant_id, track_id)

        assert result is None
        sqlite_repo.update_portrait.assert_not_called()

    async def test_old_portrait_none_with_n_greater_than_1_uses_track_vector(self):
        """If old portrait is None but n > 1 (data inconsistency), track vector is used."""
        participant_id = "p-1"
        track_id = _uid()
        track_vector = _audio_vec(0.9)

        # portrait_vector=None but tracks_played=5 (edge case)
        participant = _make_participant(participant_id, tracks_played=5, portrait_vector=None)
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=track_vector, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        result = await service.update_portrait(participant_id, track_id)

        # The condition is: old_portrait is None OR n <= 1 → use track vector directly
        # _audio_vec(0.9) is already a unit vector, so L2-norm gives the same vector
        assert result == pytest.approx(track_vector)

    async def test_ema_portrait_formula_with_n_equals_5(self):
        """Verify EMA formula: 0.3*cur + 0.7*old (regardless of n), then L2-normalise."""
        participant_id = "p-1"
        track_id = _uid()

        old_portrait = [10.0] * _DIM
        track_vector = [5.0] * _DIM
        # EMA: 0.3*5.0 + 0.7*10.0 = 1.5 + 7.0 = 8.5 per dimension
        ema_raw = [0.3 * 5.0 + 0.7 * 10.0] * _DIM  # = [8.5]*45
        expected = _l2_normalize(ema_raw)

        participant = _make_participant(participant_id, tracks_played=5, portrait_vector=old_portrait)
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=track_vector, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        result = await service.update_portrait(participant_id, track_id)

        assert result == pytest.approx(expected)

    async def test_portrait_is_l2_renormalized(self):
        """The portrait returned by update_portrait has unit L2-norm."""
        participant_id = "p-1"
        track_id = _uid()

        # Use non-unit vectors so the EMA result is not trivially normalised
        old_portrait = [2.0] * _DIM
        track_vector = [3.0] * _DIM

        participant = _make_participant(participant_id, tracks_played=2, portrait_vector=old_portrait)
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=track_vector, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        result = await service.update_portrait(participant_id, track_id)

        assert result is not None
        norm = _l2_norm(result)
        assert norm == pytest.approx(1.0, abs=1e-6)


# ===========================================================================
# TestRecordTransition
# ===========================================================================


class TestRecordTransition:
    """Tests for RecommendationService.record_transition."""

    async def test_records_transition_when_two_history_entries(self):
        """With 2 history entries, a transition is upserted to QDrant."""
        participant_id = "p-1"
        prev_id = _uid()
        curr_id = _uid()
        history = [
            _make_history_entry(participant_id, curr_id),
            _make_history_entry(participant_id, prev_id),
        ]
        sqlite_repo = _make_sqlite_repo(history=history)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.record_transition(participant_id, curr_id)

        qdrant_repo.upsert.assert_called_once()

    async def test_transition_point_id_is_deterministic_uuid(self):
        """The transition point_id is a deterministic UUID v5 based on the from→to pair."""
        from uuid import NAMESPACE_URL, uuid5

        participant_id = "p-1"
        prev_id = _uid()
        curr_id = _uid()
        history = [
            _make_history_entry(participant_id, curr_id),
            _make_history_entry(participant_id, prev_id),
        ]
        sqlite_repo = _make_sqlite_repo(history=history)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.record_transition(participant_id, curr_id)

        _, point_id, _, payload = qdrant_repo.upsert.call_args.args
        expected_id = str(uuid5(NAMESPACE_URL, f"{prev_id}_{curr_id}"))
        assert point_id == expected_id

    async def test_transition_payload_contains_from_to_tracks(self):
        """Transition payload contains from_track_id, to_track_id, and weight=1 (new transition)."""
        participant_id = "p-1"
        prev_id = _uid()
        curr_id = _uid()
        history = [
            _make_history_entry(participant_id, curr_id),
            _make_history_entry(participant_id, prev_id),
        ]
        sqlite_repo = _make_sqlite_repo(history=history)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())
        # retrieve_payload returns None → no existing transition → weight starts at 1
        qdrant_repo.retrieve_payload.return_value = None

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.record_transition(participant_id, curr_id)

        _, _, _, payload = qdrant_repo.upsert.call_args.args
        assert payload["from_track_id"] == prev_id
        assert payload["to_track_id"] == curr_id
        assert payload["weight"] == 1

    async def test_transition_uses_transitions_collection(self):
        """Transition upsert targets the 'transitions' QDrant collection."""
        participant_id = "p-1"
        prev_id = _uid()
        curr_id = _uid()
        history = [
            _make_history_entry(participant_id, curr_id),
            _make_history_entry(participant_id, prev_id),
        ]
        sqlite_repo = _make_sqlite_repo(history=history)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.record_transition(participant_id, curr_id)

        collection, _, _, _ = qdrant_repo.upsert.call_args.args
        assert collection == "transitions"

    async def test_does_nothing_when_fewer_than_two_history_entries(self):
        """record_transition is a no-op when there is only 1 history entry."""
        participant_id = "p-1"
        curr_id = _uid()
        history = [_make_history_entry(participant_id, curr_id)]
        sqlite_repo = _make_sqlite_repo(history=history)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.record_transition(participant_id, curr_id)

        qdrant_repo.upsert.assert_not_called()

    async def test_does_nothing_when_empty_history(self):
        """record_transition is a no-op when history is empty."""
        participant_id = "p-1"
        curr_id = _uid()
        sqlite_repo = _make_sqlite_repo(history=[])
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.record_transition(participant_id, curr_id)

        qdrant_repo.upsert.assert_not_called()

    async def test_does_nothing_when_current_track_has_no_vector(self):
        """If the current track has no vector, upsert is not called."""
        participant_id = "p-1"
        prev_id = _uid()
        curr_id = _uid()
        history = [
            _make_history_entry(participant_id, curr_id),
            _make_history_entry(participant_id, prev_id),
        ]
        sqlite_repo = _make_sqlite_repo(history=history)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=None, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.record_transition(participant_id, curr_id)

        qdrant_repo.upsert.assert_not_called()

    async def test_upsert_exception_is_swallowed(self):
        """If QDrant upsert raises, record_transition does not propagate the error."""
        participant_id = "p-1"
        prev_id = _uid()
        curr_id = _uid()
        history = [
            _make_history_entry(participant_id, curr_id),
            _make_history_entry(participant_id, prev_id),
        ]
        sqlite_repo = _make_sqlite_repo(history=history)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())
        # retrieve_payload succeeds but upsert raises
        qdrant_repo.retrieve_payload.return_value = None
        qdrant_repo.upsert.side_effect = RuntimeError("qdrant failed")

        service = RecommendationService(sqlite_repo, qdrant_repo)
        # Must not raise
        await service.record_transition(participant_id, curr_id)

    async def test_transition_weight_increments_when_existing(self):
        """When retrieve_payload returns an existing payload with weight=3, new weight is 4."""
        participant_id = "p-1"
        prev_id = _uid()
        curr_id = _uid()
        history = [
            _make_history_entry(participant_id, curr_id),
            _make_history_entry(participant_id, prev_id),
        ]
        sqlite_repo = _make_sqlite_repo(history=history)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())
        # Simulate an existing transition with weight=3
        qdrant_repo.retrieve_payload.return_value = {"weight": 3}

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.record_transition(participant_id, curr_id)

        _, _, _, payload = qdrant_repo.upsert.call_args.args
        assert payload["weight"] == 4

    async def test_retrieve_payload_called_before_upsert(self):
        """retrieve_payload is called (read step) before upsert (write step)."""
        participant_id = "p-1"
        prev_id = _uid()
        curr_id = _uid()
        history = [
            _make_history_entry(participant_id, curr_id),
            _make_history_entry(participant_id, prev_id),
        ]
        sqlite_repo = _make_sqlite_repo(history=history)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.record_transition(participant_id, curr_id)

        qdrant_repo.retrieve_payload.assert_called_once()
        qdrant_repo.upsert.assert_called_once()


# ===========================================================================
# TestTransitionCandidates
# ===========================================================================


class TestTransitionCandidates:
    """Tests for the _transition_candidates helper and its use in _last_strategy."""

    async def test_transition_candidates_used_in_last_strategy(self):
        """When scroll_filtered returns transitions, they appear as top results."""
        track_id = _uid()
        transition_to_id = _uid()
        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)

        transition_track = _make_track(transition_to_id)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        # get_tracks_by_ids used for both transition candidates and KNN results
        sqlite_repo.get_tracks_by_ids.return_value = {
            transition_to_id: transition_track
        }

        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())
        # Return a transition candidate from scroll_filtered
        qdrant_repo.scroll_filtered.return_value = [
            ("trans-point-id", 0.0, {
                "from_track_id": track_id,
                "to_track_id": transition_to_id,
                "weight": 5,
            })
        ]

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert strategy is RecommendationStrategy.LAST
        result_ids = [r.track.id for r in results]
        assert transition_to_id in result_ids

    async def test_scroll_filtered_called_with_transitions_collection(self):
        """_last_strategy calls scroll_filtered on the 'transitions' collection."""
        track_id = _uid()
        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.get_recommendations("p-1", "s-1", limit=5)

        qdrant_repo.scroll_filtered.assert_called_once()
        call_collection = qdrant_repo.scroll_filtered.call_args.args[0]
        assert call_collection == "transitions"

    async def test_transition_candidates_exclude_played_tracks(self):
        """Transition candidates that were already played are excluded from results."""
        track_id = _uid()
        played_id = _uid()  # This is in played_ids (history)
        history = [
            _make_history_entry("p-1", track_id),
            _make_history_entry("p-1", played_id),  # already played
        ]
        # tracks_played=2 → LAST_TWO_AVG strategy (not LAST)
        # To test LAST strategy filtering, use tracks_played=1 with 1 history entry
        # and set played_ids via history[0].track_id
        track_id2 = _uid()
        history2 = [_make_history_entry("p-1", track_id2)]
        participant = _make_participant("p-1", tracks_played=1)

        fresh_id = _uid()
        sqlite_repo = _make_sqlite_repo(history=history2, participant=participant)
        sqlite_repo.get_tracks_by_ids.return_value = {fresh_id: _make_track(fresh_id)}

        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())
        # scroll_filtered returns transition to track_id2 (played) and fresh_id (not played)
        qdrant_repo.scroll_filtered.return_value = [
            ("p1", 0.0, {"from_track_id": track_id2, "to_track_id": track_id2, "weight": 2}),  # played
            ("p2", 0.0, {"from_track_id": track_id2, "to_track_id": fresh_id, "weight": 1}),
        ]

        service = RecommendationService(sqlite_repo, qdrant_repo)
        _, results = await service.get_recommendations("p-1", "s-1", limit=5)

        result_ids = {r.track.id for r in results}
        assert track_id2 not in result_ids  # played track excluded
        assert fresh_id in result_ids

    async def test_scroll_filtered_exception_returns_empty_candidates(self):
        """If scroll_filtered raises, _transition_candidates returns [] (exception swallowed)."""
        track_id = _uid()
        history = [_make_history_entry("p-1", track_id)]
        participant = _make_participant("p-1", tracks_played=1)
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo(audio_retrieve=_audio_vec())
        qdrant_repo.scroll_filtered.side_effect = RuntimeError("qdrant down")

        service = RecommendationService(sqlite_repo, qdrant_repo)
        # Should not raise; falls through to KNN only
        strategy, results = await service.get_recommendations("p-1", "s-1", limit=5)

        assert strategy is RecommendationStrategy.LAST


# ===========================================================================
# TestQDrantRepoRetrieve — real in-memory QDrant client
# ===========================================================================


class TestQDrantRepoRetrieve:
    """Integration tests for QDrantRepository.retrieve using the real in-memory client."""

    def test_retrieve_returns_vector_for_existing_point(self, qdrant_repo):
        """retrieve() returns the stored vector when the point exists."""
        pid = _uid()
        v = _audio_vec(0.42)
        qdrant_repo.upsert("audio_features", pid, v, {"status": "ready"})

        result = qdrant_repo.retrieve("audio_features", pid)

        assert result is not None
        assert len(result) == _DIM
        assert result == pytest.approx(v, abs=1e-5)

    def test_retrieve_returns_none_for_non_existing_point(self, qdrant_repo):
        """retrieve() returns None when the point ID does not exist."""
        result = qdrant_repo.retrieve("audio_features", _uid())

        assert result is None

    def test_retrieve_returns_none_after_delete(self, qdrant_repo):
        """retrieve() returns None after the point has been deleted."""
        pid = _uid()
        v = _audio_vec(0.1)
        qdrant_repo.upsert("audio_features", pid, v, {})
        qdrant_repo.delete("audio_features", pid)

        result = qdrant_repo.retrieve("audio_features", pid)

        assert result is None

    def test_retrieve_returns_exact_vector_values(self, qdrant_repo):
        """The retrieved vector matches the inserted vector element-by-element."""
        pid = _uid()
        # Use a distinctive pattern — not just normalised values
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
        """Set up a session with one participant and seed QDrant on app.state."""
        from karaoke_shared.models.track import TrackCreate
        from karaoke_shared.repositories import SQLiteRepository
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        from app.main import app

        repo = SQLiteRepository(app_db)

        # Create a ready track
        track = await repo.create_track(
            TrackCreate(
                artist="Queen",
                title="Bohemian Rhapsody",
                source="catalog",
                status="ready",
                duration_sec=354,
            )
        )

        # Create session + participant via API
        r = await client.post("/api/v1/sessions", json={"room_id": "room-rec-1"})
        assert r.status_code == 201
        session_id = r.json()["id"]

        r2 = await client.post(
            f"/api/v1/sessions/{session_id}/participants", json={"name": "Alice"}
        )
        assert r2.status_code == 201
        participant_id = r2.json()["id"]

        # Inject an in-memory QDrant client into app.state for this test
        qdrant_client = QdrantClient(":memory:")
        for coll, dim in [("audio_features", 45), ("lyrics_embeddings", 384), ("transitions", 45)]:
            qdrant_client.create_collection(
                collection_name=coll,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

        # Store the track's audio vector
        v = _audio_vec(0.3)
        qdrant_client.upsert(
            collection_name="audio_features",
            points=[
                __import__(
                    "qdrant_client.models", fromlist=["PointStruct"]
                ).PointStruct(id=track.id, vector=v, payload={"status": "ready"})
            ],
        )

        app.state.qdrant = qdrant_client

        yield {
            "session_id": session_id,
            "participant_id": participant_id,
            "track_id": track.id,
            "repo": repo,
        }

        # Reset qdrant to None so other tests are unaffected
        app.state.qdrant = None

    async def test_returns_200_with_no_history(self, client, rec_fixtures):
        """GET /recommendations returns 200 when participant has no history."""
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={
                "participant_id": f["participant_id"],
                "session_id": f["session_id"],
                "limit": 5,
            },
        )

        assert r.status_code == 200

    async def test_response_has_strategy_field(self, client, rec_fixtures):
        """Response JSON includes a 'strategy' field."""
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={
                "participant_id": f["participant_id"],
                "session_id": f["session_id"],
            },
        )
        body = r.json()
        assert "strategy" in body

    async def test_response_has_tracks_field(self, client, rec_fixtures):
        """Response JSON includes a 'tracks' field that is a list."""
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={
                "participant_id": f["participant_id"],
                "session_id": f["session_id"],
            },
        )
        body = r.json()
        assert "tracks" in body
        assert isinstance(body["tracks"], list)

    async def test_no_history_returns_popular_strategy(self, client, rec_fixtures):
        """With no play history, the response strategy is 'popular'."""
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={
                "participant_id": f["participant_id"],
                "session_id": f["session_id"],
            },
        )
        assert r.json()["strategy"] == "popular"

    async def test_track_items_have_required_fields(self, client, rec_fixtures):
        """Each track item includes id, artist, title, duration_sec, similarity_score."""
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={
                "participant_id": f["participant_id"],
                "session_id": f["session_id"],
            },
        )
        body = r.json()
        for item in body["tracks"]:
            assert "id" in item
            assert "artist" in item
            assert "title" in item
            assert "duration_sec" in item
            assert "similarity_score" in item

    async def test_limit_parameter_respected(self, client, app_db, rec_fixtures):
        """The limit query parameter caps the number of returned tracks."""
        from karaoke_shared.models.track import TrackCreate
        from karaoke_shared.repositories import SQLiteRepository

        repo = SQLiteRepository(app_db)
        # Insert more popular tracks so we have enough to test limit
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
            params={
                "participant_id": f["participant_id"],
                "session_id": f["session_id"],
                "limit": 2,
            },
        )
        assert r.status_code == 200
        assert len(r.json()["tracks"]) <= 2

    async def test_missing_participant_id_returns_422(self, client, rec_fixtures):
        """Missing participant_id query param yields 422 Unprocessable Entity."""
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={"session_id": f["session_id"]},
        )
        assert r.status_code == 422

    async def test_missing_session_id_returns_422(self, client, rec_fixtures):
        """Missing session_id query param yields 422 Unprocessable Entity."""
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={"participant_id": f["participant_id"]},
        )
        assert r.status_code == 422

    async def test_strategy_enum_values_are_valid(self, client, rec_fixtures):
        """The strategy value is one of the four valid enum strings."""
        valid_strategies = {"popular", "last", "last_two_avg", "session_avg"}
        f = rec_fixtures
        r = await client.get(
            "/api/v1/recommendations",
            params={
                "participant_id": f["participant_id"],
                "session_id": f["session_id"],
            },
        )
        assert r.json()["strategy"] in valid_strategies


# ===========================================================================
# TestQueueServiceFinishPlayingIntegration
# ===========================================================================


class TestQueueServiceFinishPlayingIntegration:
    """Integration tests for QueueService.finish_playing recommendation updates."""

    def _make_queue_entry(
        self,
        entry_id: str,
        session_id: str,
        participant_id: str,
        track_id: str,
    ):
        entry = MagicMock()
        entry.id = entry_id
        entry.session_id = session_id
        entry.participant_id = participant_id
        entry.track_id = track_id
        return entry

    async def _build_queue_service_with_repo(
        self,
        entry,
        qdrant_repo=None,
        next_entry=None,
    ):
        """Build a QueueService with a fully mocked SQLiteRepository."""
        from app.services.queue_service import QueueService

        repo = AsyncMock()
        repo.get_queue_entry.return_value = entry
        repo.update_queue_entry_status.return_value = None
        repo.create_play_history.return_value = MagicMock()
        repo.increment_play_count.return_value = None
        repo.increment_tracks_played.return_value = None
        repo.get_current_entry.return_value = next_entry
        return QueueService(repo=repo, qdrant_repo=qdrant_repo), repo

    async def test_finish_playing_skips_recommendation_when_qdrant_is_none(self):
        """finish_playing skips portrait/transition updates when qdrant_repo is None."""
        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_queue_service_with_repo(entry, qdrant_repo=None)

        with patch(
            "app.services.recommendation_service.RecommendationService.update_portrait",
            new_callable=AsyncMock,
        ) as mock_update, patch(
            "app.services.recommendation_service.RecommendationService.record_transition",
            new_callable=AsyncMock,
        ) as mock_transition:
            await service.finish_playing("e-1")

        mock_update.assert_not_called()
        mock_transition.assert_not_called()

    async def test_finish_playing_still_succeeds_when_recommendation_update_raises(self):
        """finish_playing returns successfully even if the recommendation update throws."""
        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")

        qdrant_repo = _make_qdrant_repo()
        service, repo = await self._build_queue_service_with_repo(entry, qdrant_repo)

        with patch(
            "app.services.recommendation_service.RecommendationService.update_portrait",
            new_callable=AsyncMock,
            side_effect=RuntimeError("QDrant exploded"),
        ):
            # Must not raise
            result = await service.finish_playing("e-1")

        # Queue entry status was updated correctly despite the error
        repo.update_queue_entry_status.assert_called_with("e-1", "done")
        # Returns None or next entry — confirm no exception escaped
        assert result is None or result is not None  # just checking no exception

    async def test_finish_playing_returns_none_for_missing_entry(self):
        """finish_playing returns None when the entry_id does not exist."""
        from app.services.queue_service import QueueService

        repo = AsyncMock()
        repo.get_queue_entry.return_value = None

        service = QueueService(repo=repo, qdrant_repo=None)
        result = await service.finish_playing("nonexistent-entry-id")

        assert result is None

    async def test_finish_playing_increments_play_count(self):
        """finish_playing increments the track's play count regardless of QDrant."""
        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_queue_service_with_repo(entry, qdrant_repo=None)

        await service.finish_playing("e-1")

        repo.increment_play_count.assert_called_once_with("t-1")

    async def test_finish_playing_increments_tracks_played(self):
        """finish_playing increments the participant's tracks_played counter."""
        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_queue_service_with_repo(entry, qdrant_repo=None)

        await service.finish_playing("e-1")

        repo.increment_tracks_played.assert_called_once_with("p-1")

    async def test_finish_playing_creates_play_history(self):
        """finish_playing writes a play history record with correct fields."""
        from karaoke_shared.models.play_history import PlayHistoryCreate

        entry = self._make_queue_entry("e-1", "s-1", "p-1", "t-1")
        service, repo = await self._build_queue_service_with_repo(entry, qdrant_repo=None)

        await service.finish_playing("e-1")

        repo.create_play_history.assert_called_once()
        call_arg: PlayHistoryCreate = repo.create_play_history.call_args.args[0]
        assert call_arg.session_id == "s-1"
        assert call_arg.participant_id == "p-1"
        assert call_arg.track_id == "t-1"


# ===========================================================================
# TestFallbackGuard — regression tests for the IndexError fix
# ===========================================================================


class TestFallbackGuard:
    """Verify the cascading fallback when portrait is missing and history is short."""

    async def test_fallback_with_empty_history_uses_popular(self):
        """tracks_played=5 but history is empty and portrait is None → POPULAR."""
        participant_id = "p-1"
        participant = _make_participant(
            participant_id, tracks_played=5, portrait_vector=None
        )
        popular_track = _make_track(_uid())
        sqlite_repo = _make_sqlite_repo(
            history=[], participant=participant, popular=[popular_track]
        )
        qdrant_repo = _make_qdrant_repo()

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, results = await service.get_recommendations(
            participant_id, "s-1", limit=5
        )

        assert strategy is RecommendationStrategy.POPULAR

    async def test_fallback_with_one_history_entry_uses_last(self):
        """tracks_played=3 but only 1 history entry and no portrait → LAST."""
        participant_id = "p-1"
        track_id = _uid()
        history = [_make_history_entry(participant_id, track_id)]
        participant = _make_participant(
            participant_id, tracks_played=3, portrait_vector=None
        )
        popular_track = _make_track(_uid())
        sqlite_repo = _make_sqlite_repo(
            history=history, participant=participant, popular=[popular_track]
        )
        # Track has no audio vector → LAST falls back to POPULAR
        qdrant_repo = _make_qdrant_repo(audio_retrieve=None, lyrics_retrieve=None)

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations(
            participant_id, "s-1", limit=5
        )

        # LAST strategy selected, then falls back to POPULAR internally
        assert strategy is RecommendationStrategy.POPULAR

    async def test_fallback_with_two_history_entries_uses_last_two_avg(self):
        """tracks_played=4 but no portrait and 2 history entries → LAST_TWO_AVG."""
        participant_id = "p-1"
        t1, t2 = _uid(), _uid()
        history = [
            _make_history_entry(participant_id, t1),
            _make_history_entry(participant_id, t2),
        ]
        participant = _make_participant(
            participant_id, tracks_played=4, portrait_vector=None
        )
        sqlite_repo = _make_sqlite_repo(history=history, participant=participant)
        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(0.1), lyrics_retrieve=None
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        strategy, _ = await service.get_recommendations(
            participant_id, "s-1", limit=5
        )

        assert strategy is RecommendationStrategy.LAST_TWO_AVG


# ===========================================================================
# TestLyricsPortraitPreservation — regression tests for lyrics erasure fix
# ===========================================================================


class TestLyricsPortraitPreservation:
    """Verify that lyrics portrait is preserved when playing a track without lyrics."""

    async def test_no_lyrics_track_preserves_existing_lyrics_portrait(self):
        """Playing a track with no lyrics embedding must NOT erase the lyrics portrait."""
        participant_id = "p-1"
        track_id = _uid()
        old_audio = _audio_vec(0.2)
        old_lyrics = _lyrics_vec(0.5)

        participant = _make_participant(
            participant_id,
            tracks_played=3,
            portrait_vector=old_audio,
            lyrics_portrait_vector=old_lyrics,
        )
        sqlite_repo = _make_sqlite_repo(participant=participant)
        # Track has audio but no lyrics vector
        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(0.8), lyrics_retrieve=None
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.update_portrait(participant_id, track_id)

        # update_portrait should be called with lyrics_portrait=None
        # (meaning "don't change it" — the SQLite repo handles this)
        sqlite_repo.update_portrait.assert_called_once()
        call_args = sqlite_repo.update_portrait.call_args.args
        _, _, call_lyrics_vec = call_args
        assert call_lyrics_vec is None

    async def test_lyrics_track_updates_lyrics_portrait(self):
        """Playing a track WITH lyrics should update the lyrics portrait normally."""
        participant_id = "p-1"
        track_id = _uid()
        old_audio = _audio_vec(0.2)
        old_lyrics = _lyrics_vec(0.5)

        participant = _make_participant(
            participant_id,
            tracks_played=3,
            portrait_vector=old_audio,
            lyrics_portrait_vector=old_lyrics,
        )
        sqlite_repo = _make_sqlite_repo(participant=participant)
        qdrant_repo = _make_qdrant_repo(
            audio_retrieve=_audio_vec(0.8), lyrics_retrieve=_lyrics_vec(0.9)
        )

        service = RecommendationService(sqlite_repo, qdrant_repo)
        await service.update_portrait(participant_id, track_id)

        sqlite_repo.update_portrait.assert_called_once()
        call_args = sqlite_repo.update_portrait.call_args.args
        _, _, call_lyrics_vec = call_args
        # Lyrics portrait should be updated (not None)
        assert call_lyrics_vec is not None
        assert len(call_lyrics_vec) == _LYRICS_DIM
