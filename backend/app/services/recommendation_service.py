"""Recommendation service — session-level track recommendations.

Currently uses a single strategy:
- ``popular``: mix of most-played + random tracks from the catalog.

Future phases (R1–R4) will add cluster-based recommendations with
popularity re-ranking, MMR diversity, and mood-tag filtering.

Vector searches use **weighted fusion** of two QDrant collections:
- ``audio_features`` (45-dim librosa, z-score normalised) — weight 0.7
- ``lyrics_embeddings`` (384-dim SentenceTransformer) — weight 0.3
"""

from __future__ import annotations

import asyncio

import structlog
from karaoke_shared.constants import (
    COLLECTION_AUDIO_FEATURES,
    COLLECTION_LYRICS_EMBEDDINGS,
)
from karaoke_shared.models.recommendation import RecommendationStrategy
from karaoke_shared.models.track import Track
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

logger = structlog.get_logger(__name__)

_AUDIO_COLLECTION = COLLECTION_AUDIO_FEATURES
_LYRICS_COLLECTION = COLLECTION_LYRICS_EMBEDDINGS

# Fusion weights for audio and lyrics KNN scores.
_AUDIO_WEIGHT = 0.7
_LYRICS_WEIGHT = 0.3


class RecommendedTrack:
    """A track with its similarity score."""

    __slots__ = ("track", "similarity_score")

    def __init__(self, track: Track, similarity_score: float) -> None:
        self.track = track
        self.similarity_score = similarity_score


class RecommendationService:
    """Session-level recommendation engine.

    Args:
        sqlite_repo: Repository for reading tracks and play history.
        qdrant_repo: Repository for vector similarity searches.
    """

    def __init__(
        self,
        sqlite_repo: SQLiteRepository,
        qdrant_repo: QDrantRepository,
    ) -> None:
        self.sqlite_repo = sqlite_repo
        self.qdrant_repo = qdrant_repo

    async def get_recommendations(
        self,
        session_id: str,
        limit: int = 5,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Return recommendations for a session.

        Currently returns popular tracks only.  Future phases will add
        cluster-based recommendations when 1+ tracks have been played.
        """
        history = await self.sqlite_repo.get_history_by_session(session_id)
        played_ids = {entry.track_id for entry in history}
        return await self._popular_strategy(played_ids, limit)

    # ------------------------------------------------------------------
    # Strategies
    # ------------------------------------------------------------------

    async def _popular_strategy(
        self, played_ids: set[str], limit: int
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Return a mix of popular and random tracks.

        70% of slots go to the most-played tracks (crowd favourites),
        30% go to random picks to break the popularity feedback loop
        and give less-played tracks a chance.
        """
        n_top = max(1, int(limit * 0.7))
        n_random = limit - n_top

        # Fetch extra to compensate for overlap and already-played filtering.
        extra = len(played_ids) + limit
        top_tracks = await self.sqlite_repo.list_popular(limit=n_top + extra)
        random_tracks = await self.sqlite_repo.list_random(limit=n_random + extra)

        seen: set[str] = set(played_ids)
        results: list[RecommendedTrack] = []

        # Top-played first.
        for t in top_tracks:
            if t.id not in seen:
                seen.add(t.id)
                results.append(RecommendedTrack(track=t, similarity_score=0.0))
                if len(results) >= n_top:
                    break

        # Random fill.
        for t in random_tracks:
            if t.id not in seen:
                seen.add(t.id)
                results.append(RecommendedTrack(track=t, similarity_score=0.0))
                if len(results) >= limit:
                    break

        return RecommendationStrategy.POPULAR, results

    # ------------------------------------------------------------------
    # Helpers (kept for future phases R1-R4)
    # ------------------------------------------------------------------

    async def _get_track_vector(self, track_id: str) -> list[float] | None:
        """Retrieve the audio_features vector for a track from QDrant."""
        try:
            return await asyncio.to_thread(
                self.qdrant_repo.retrieve, _AUDIO_COLLECTION, track_id
            )
        except Exception as exc:
            logger.warning(
                "vector_retrieval_failed",
                collection=_AUDIO_COLLECTION,
                track_id=track_id,
                error=str(exc),
            )
            return None

    async def _get_track_lyrics_vector(self, track_id: str) -> list[float] | None:
        """Retrieve the lyrics_embeddings vector for a track from QDrant."""
        try:
            return await asyncio.to_thread(
                self.qdrant_repo.retrieve, _LYRICS_COLLECTION, track_id
            )
        except Exception as exc:
            logger.warning(
                "vector_retrieval_failed",
                collection=_LYRICS_COLLECTION,
                track_id=track_id,
                error=str(exc),
            )
            return None

    async def _fused_knn_search(
        self,
        audio_vector: list[float] | None,
        lyrics_vector: list[float] | None,
        exclude_ids: set[str],
        limit: int,
    ) -> list[RecommendedTrack]:
        """Run weighted fusion KNN across audio and lyrics collections.

        Searches both collections in parallel (when vectors are available),
        merges results by track ID with weighted scores:
        ``fused = 0.7 * audio_score + 0.3 * lyrics_score``

        Falls back to single-collection search when one vector is missing.
        """
        oversample = limit + len(exclude_ids) + 10

        # Launch parallel KNN searches.
        audio_task = self._knn_raw(
            _AUDIO_COLLECTION, audio_vector, oversample
        ) if audio_vector else _empty_coro()

        lyrics_task = self._knn_raw(
            _LYRICS_COLLECTION, lyrics_vector, oversample
        ) if lyrics_vector else _empty_coro()

        audio_hits, lyrics_hits = await asyncio.gather(audio_task, lyrics_task)

        # Build score maps (point_id → score).
        audio_scores: dict[str, float] = {
            pid: score for pid, score, _ in audio_hits
            if pid not in exclude_ids
        }
        lyrics_scores: dict[str, float] = {
            pid: score for pid, score, _ in lyrics_hits
            if pid not in exclude_ids
        }

        # Merge: union of all candidate IDs.
        all_ids = set(audio_scores) | set(lyrics_scores)
        if not all_ids:
            return []

        # Compute fused scores.
        if audio_scores and lyrics_scores:
            # Both collections returned results — weighted fusion with
            # per-candidate normalisation so that candidates appearing in
            # only one collection are not penalised.
            fused: list[tuple[str, float]] = []
            for pid in all_ids:
                has_audio = pid in audio_scores
                has_lyrics = pid in lyrics_scores
                a = audio_scores.get(pid, 0.0)
                l = lyrics_scores.get(pid, 0.0)
                w = (
                    (_AUDIO_WEIGHT if has_audio else 0.0)
                    + (_LYRICS_WEIGHT if has_lyrics else 0.0)
                )
                fused.append(
                    (pid, (_AUDIO_WEIGHT * a + _LYRICS_WEIGHT * l) / w if w > 0 else 0.0)
                )
        elif audio_scores:
            # Audio only.
            fused = [(pid, score) for pid, score in audio_scores.items()]
        else:
            # Lyrics only.
            fused = [(pid, score) for pid, score in lyrics_scores.items()]

        # Sort by fused score descending, take top-N.
        fused.sort(key=lambda x: x[1], reverse=True)
        top = fused[:limit]

        if not top:
            return []

        # Single batch SQLite query.
        candidate_ids = [pid for pid, _ in top]
        tracks_map = await self.sqlite_repo.get_tracks_by_ids(candidate_ids)

        results: list[RecommendedTrack] = []
        for pid, score in top:
            track = tracks_map.get(pid)
            if track is not None:
                results.append(RecommendedTrack(track=track, similarity_score=score))

        return results

    async def _knn_raw(
        self,
        collection: str,
        vector: list[float],
        limit: int,
    ) -> list[tuple[str, float, dict]]:
        """Run raw KNN search in a single QDrant collection."""
        try:
            return await asyncio.to_thread(
                self.qdrant_repo.search,
                collection,
                vector,
                limit=limit,
                filters={"status": "ready"},
            )
        except Exception as exc:
            logger.warning("knn_search_failed", collection=collection, error=str(exc))
            return []


async def _empty_coro() -> list:
    """Return an empty list — used as a no-op coroutine for asyncio.gather."""
    return []
