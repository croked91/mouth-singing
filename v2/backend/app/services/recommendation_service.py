"""Recommendation service — content-based track recommendations.

Strategies are selected automatically based on how many tracks the
participant has already sung in the current session:

- 0 tracks → ``popular``: most-played tracks from the catalog.
- 1 track  → ``last``: KNN on the audio feature vector of the last track.
- 2 tracks → ``last_two_avg``: KNN on the average of the last two vectors.
- 3+ tracks → ``session_avg``: KNN on the participant's portrait vector.

All vector searches run against the ``audio_features`` QDrant collection
(45-dimensional librosa features).  Already-played tracks are excluded.
"""

from __future__ import annotations

import asyncio

import structlog
from karaoke_shared.models.recommendation import RecommendationStrategy
from karaoke_shared.models.track import Track
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

logger = structlog.get_logger(__name__)

_AUDIO_COLLECTION = "audio_features"
_TRANSITIONS_COLLECTION = "transitions"


class RecommendedTrack:
    """A track with its similarity score."""

    __slots__ = ("track", "similarity_score")

    def __init__(self, track: Track, similarity_score: float) -> None:
        self.track = track
        self.similarity_score = similarity_score


class RecommendationService:
    """Content-based recommendation engine.

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
        participant_id: str,
        session_id: str,
        limit: int = 10,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Return recommendations for a participant.

        The strategy is chosen automatically based on the number of tracks
        the participant has played in the current session.

        Returns:
            A tuple of (strategy, recommended_tracks).
        """
        # Fetch play history for this participant (most recent first).
        history = await self.sqlite_repo.get_history_by_participant(
            participant_id, limit=50
        )

        # IDs of tracks already played — these will be excluded.
        played_ids = {entry.track_id for entry in history}

        tracks_played = len(history)

        if tracks_played == 0:
            return await self._popular_strategy(played_ids, limit)

        if tracks_played == 1:
            return await self._last_strategy(history[0].track_id, played_ids, limit)

        if tracks_played == 2:
            return await self._last_two_avg_strategy(
                history[0].track_id, history[1].track_id, played_ids, limit
            )

        # 3+ tracks: use the participant's portrait vector.
        participant = await self.sqlite_repo.get_participant(participant_id)
        if participant and participant.portrait_vector:
            return await self._session_avg_strategy(
                participant.portrait_vector, played_ids, limit
            )

        # Fallback: if portrait is somehow missing, use last two.
        return await self._last_two_avg_strategy(
            history[0].track_id, history[1].track_id, played_ids, limit
        )

    # ------------------------------------------------------------------
    # Strategies
    # ------------------------------------------------------------------

    async def _popular_strategy(
        self, played_ids: set[str], limit: int
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Return most-played tracks from the catalog."""
        # Fetch more than needed so we can filter out played tracks.
        tracks = await self.sqlite_repo.list_popular(limit=limit + len(played_ids))
        results = [
            RecommendedTrack(track=t, similarity_score=0.0)
            for t in tracks
            if t.id not in played_ids
        ][:limit]
        return RecommendationStrategy.POPULAR, results

    async def _last_strategy(
        self, track_id: str, played_ids: set[str], limit: int
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """KNN on the audio feature vector of the last played track."""
        vector = await self._get_track_vector(track_id)
        if vector is None:
            return await self._popular_strategy(played_ids, limit)

        results = await self._knn_search(vector, played_ids, limit)
        return RecommendationStrategy.LAST, results

    async def _last_two_avg_strategy(
        self,
        track_id_1: str,
        track_id_2: str,
        played_ids: set[str],
        limit: int,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """KNN on the average of the two most recent track vectors."""
        v1 = await self._get_track_vector(track_id_1)
        v2 = await self._get_track_vector(track_id_2)

        if v1 is None and v2 is None:
            return await self._popular_strategy(played_ids, limit)
        if v1 is None:
            results = await self._knn_search(v2, played_ids, limit)
            return RecommendationStrategy.LAST_TWO_AVG, results
        if v2 is None:
            results = await self._knn_search(v1, played_ids, limit)
            return RecommendationStrategy.LAST_TWO_AVG, results

        avg = [(a + b) / 2.0 for a, b in zip(v1, v2)]
        results = await self._knn_search(avg, played_ids, limit)
        return RecommendationStrategy.LAST_TWO_AVG, results

    async def _session_avg_strategy(
        self,
        portrait_vector: list[float],
        played_ids: set[str],
        limit: int,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """KNN on the participant's session portrait vector."""
        results = await self._knn_search(portrait_vector, played_ids, limit)
        return RecommendationStrategy.SESSION_AVG, results

    # ------------------------------------------------------------------
    # Helpers
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
                track_id=track_id,
                error=str(exc),
            )
            return None

    async def _knn_search(
        self,
        vector: list[float],
        exclude_ids: set[str],
        limit: int,
    ) -> list[RecommendedTrack]:
        """Run KNN search in QDrant and return enriched results.

        Fetches extra candidates to account for filtering out already-played
        tracks, then resolves each hit to a full Track from SQLite.
        """
        try:
            hits = await asyncio.to_thread(
                self.qdrant_repo.search,
                _AUDIO_COLLECTION,
                vector,
                limit=limit + len(exclude_ids),
                filters={"status": "ready"},
            )
        except Exception as exc:
            logger.warning("knn_search_failed", error=str(exc))
            return []

        results: list[RecommendedTrack] = []
        for point_id, score, _payload in hits:
            if point_id in exclude_ids:
                continue
            if len(results) >= limit:
                break

            track = await self.sqlite_repo.get_track(point_id)
            if track is not None:
                results.append(RecommendedTrack(track=track, similarity_score=score))

        return results

    async def update_portrait(
        self, participant_id: str, track_id: str
    ) -> list[float] | None:
        """Update the participant's portrait vector after finishing a track.

        Computes a running average: ``new = (old * (n-1) + current) / n``
        where *n* is the new tracks_played count.

        Returns the updated portrait vector, or ``None`` if the track has
        no vector in QDrant.
        """
        current_vector = await self._get_track_vector(track_id)
        if current_vector is None:
            return None

        participant = await self.sqlite_repo.get_participant(participant_id)
        if participant is None:
            return None

        n = participant.tracks_played  # Already incremented by finish_playing.
        old_portrait = participant.portrait_vector

        if old_portrait is None or n <= 1:
            # First track — the portrait IS the track's vector.
            new_portrait = current_vector
        else:
            # Running average.
            new_portrait = [
                (old * (n - 1) + cur) / n
                for old, cur in zip(old_portrait, current_vector)
            ]

        await self.sqlite_repo.update_portrait(participant_id, new_portrait)
        return new_portrait

    async def record_transition(
        self, participant_id: str, current_track_id: str
    ) -> None:
        """Record a track transition for collaborative filtering.

        Looks at the participant's play history to find the previous track.
        If found, upserts a transition vector in QDrant's ``transitions``
        collection using the current track's audio_features as the vector
        and the from/to pair in the payload.
        """
        history = await self.sqlite_repo.get_history_by_participant(
            participant_id, limit=2
        )

        # Need at least 2 entries: current (just recorded) + previous.
        if len(history) < 2:
            return

        previous_track_id = history[1].track_id  # history is most-recent-first
        current_vector = await self._get_track_vector(current_track_id)
        if current_vector is None:
            return

        # Use a deterministic point ID based on the from→to pair.
        transition_id = f"{previous_track_id}_{current_track_id}"

        try:
            await asyncio.to_thread(
                self.qdrant_repo.upsert,
                _TRANSITIONS_COLLECTION,
                transition_id,
                current_vector,
                {
                    "from_track_id": previous_track_id,
                    "to_track_id": current_track_id,
                    "weight": 1,
                },
            )
            logger.info(
                "transition_recorded",
                from_track=previous_track_id,
                to_track=current_track_id,
            )
        except Exception as exc:
            logger.warning("transition_record_failed", error=str(exc))
