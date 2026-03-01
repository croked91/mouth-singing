"""Recommendation service — content-based + collaborative track recommendations.

Strategies are selected automatically based on how many tracks the
participant has already sung in the current session:

- 0 tracks → ``popular``: 70% most-played + 30% random from catalog.
- 1 track  → ``last``: transition graph candidates + KNN on last track.
- 2 tracks → ``last_two_avg``: KNN on the average of the last two vectors.
- 3+ tracks → ``session_avg``: KNN on the participant's EMA portrait vector.

All vector searches run against the ``audio_features`` QDrant collection
(45-dimensional librosa features, z-score normalised).  Already-played
tracks are excluded.

Portrait vectors are updated using Exponential Moving Average (EMA) with
``alpha = 0.3`` and L2-renormalised after each update.
"""

from __future__ import annotations

import asyncio
from uuid import NAMESPACE_URL, uuid5

import numpy as np
import structlog
from karaoke_shared.models.recommendation import RecommendationStrategy
from karaoke_shared.models.track import Track
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

logger = structlog.get_logger(__name__)

_AUDIO_COLLECTION = "audio_features"
_TRANSITIONS_COLLECTION = "transitions"

# Weight of the most recent track in EMA portrait update.
# Effective window ≈ 1/alpha ≈ 3-4 most recent tracks.
_EMA_ALPHA = 0.3


class RecommendedTrack:
    """A track with its similarity score."""

    __slots__ = ("track", "similarity_score")

    def __init__(self, track: Track, similarity_score: float) -> None:
        self.track = track
        self.similarity_score = similarity_score


class RecommendationService:
    """Content-based + collaborative recommendation engine.

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
        # Fetch participant once — used for tracks_played count and portrait.
        participant = await self.sqlite_repo.get_participant(participant_id)
        tracks_played = participant.tracks_played if participant else 0

        # Fetch play history for exclusion and vector lookups.
        history = await self.sqlite_repo.get_history_by_participant(
            participant_id, limit=50
        )
        played_ids = {entry.track_id for entry in history}

        if tracks_played == 0:
            return await self._popular_strategy(played_ids, limit)

        if tracks_played == 1:
            return await self._last_strategy(history[0].track_id, played_ids, limit)

        if tracks_played == 2:
            return await self._last_two_avg_strategy(
                history[0].track_id, history[1].track_id, played_ids, limit
            )

        # 3+ tracks: use the participant's EMA portrait vector.
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

    async def _last_strategy(
        self, track_id: str, played_ids: set[str], limit: int
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Transition graph candidates + KNN on last track's audio features.

        If the transition graph has data for this track, those candidates
        come first (real human signal). Remaining slots are filled by KNN.
        """
        # Transition candidates (up to half the slots).
        trans_limit = limit // 2
        trans_ids = await self._transition_candidates(track_id, played_ids, trans_limit)

        # KNN for the rest.
        vector = await self._get_track_vector(track_id)
        if vector is None and not trans_ids:
            return await self._popular_strategy(played_ids, limit)

        knn_results: list[RecommendedTrack] = []
        if vector is not None:
            knn_exclude = played_ids | set(trans_ids)
            knn_results = await self._knn_search(
                vector, knn_exclude, limit - len(trans_ids)
            )

        # Resolve transition candidates to full tracks.
        trans_results: list[RecommendedTrack] = []
        if trans_ids:
            tracks_map = await self.sqlite_repo.get_tracks_by_ids(trans_ids)
            for tid in trans_ids:
                track = tracks_map.get(tid)
                if track is not None:
                    trans_results.append(
                        RecommendedTrack(track=track, similarity_score=1.0)
                    )

        return RecommendationStrategy.LAST, trans_results + knn_results

    async def _last_two_avg_strategy(
        self,
        track_id_1: str,
        track_id_2: str,
        played_ids: set[str],
        limit: int,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """KNN on the L2-normalised average of the two most recent vectors."""
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

        avg = np.array([(a + b) / 2.0 for a, b in zip(v1, v2)], dtype=np.float64)
        norm = np.linalg.norm(avg)
        if norm > 1e-8:
            avg = avg / norm
        results = await self._knn_search(avg.tolist(), played_ids, limit)
        return RecommendationStrategy.LAST_TWO_AVG, results

    async def _session_avg_strategy(
        self,
        portrait_vector: list[float],
        played_ids: set[str],
        limit: int,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """KNN on the participant's session EMA portrait vector."""
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

        Uses a single batch SQLite query instead of N+1 individual lookups.
        """
        try:
            hits = await asyncio.to_thread(
                self.qdrant_repo.search,
                _AUDIO_COLLECTION,
                vector,
                limit=limit + len(exclude_ids) + 10,
                filters={"status": "ready"},
            )
        except Exception as exc:
            logger.warning("knn_search_failed", error=str(exc))
            return []

        # Collect candidate IDs (excluding already-played).
        candidate_ids: list[str] = []
        candidate_scores: dict[str, float] = {}
        for point_id, score, _payload in hits:
            if point_id not in exclude_ids and len(candidate_ids) < limit:
                candidate_ids.append(point_id)
                candidate_scores[point_id] = score

        if not candidate_ids:
            return []

        # Single batch SQLite query.
        tracks_map = await self.sqlite_repo.get_tracks_by_ids(candidate_ids)

        results: list[RecommendedTrack] = []
        for cid in candidate_ids:
            track = tracks_map.get(cid)
            if track is not None:
                results.append(
                    RecommendedTrack(
                        track=track, similarity_score=candidate_scores[cid]
                    )
                )

        return results

    async def _transition_candidates(
        self, from_track_id: str, exclude_ids: set[str], limit: int
    ) -> list[str]:
        """Return to_track_ids from the transition graph, sorted by weight.

        Queries the ``transitions`` collection using a payload filter on
        ``from_track_id``, then sorts results by weight descending.
        Returns an empty list if no transitions exist.
        """
        try:
            hits = await asyncio.to_thread(
                self.qdrant_repo.scroll_filtered,
                _TRANSITIONS_COLLECTION,
                {"from_track_id": from_track_id},
                limit * 4,
            )
        except Exception:
            return []

        # Sort by weight descending (higher = more times played in sequence).
        hits.sort(key=lambda h: h[2].get("weight", 1), reverse=True)

        result: list[str] = []
        for _, _, payload in hits:
            to_id = payload.get("to_track_id")
            if to_id and to_id not in exclude_ids and to_id not in result:
                result.append(to_id)
                if len(result) >= limit:
                    break
        return result

    async def update_portrait(
        self, participant_id: str, track_id: str
    ) -> list[float] | None:
        """Update the participant's portrait vector after finishing a track.

        Uses Exponential Moving Average (EMA):
        ``new = alpha * current + (1 - alpha) * old``

        The result is L2-renormalised to stay on the unit sphere.

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
            # Exponential Moving Average.
            new_portrait = [
                _EMA_ALPHA * cur + (1 - _EMA_ALPHA) * old
                for old, cur in zip(old_portrait, current_vector)
            ]

        # L2-renormalise (EMA of unit vectors is not a unit vector).
        arr = np.array(new_portrait, dtype=np.float64)
        norm = np.linalg.norm(arr)
        if norm > 1e-8:
            arr = arr / norm
        new_portrait = arr.tolist()

        await self.sqlite_repo.update_portrait(participant_id, new_portrait)
        return new_portrait

    async def record_transition(
        self, participant_id: str, current_track_id: str
    ) -> None:
        """Record a track transition for collaborative filtering.

        Looks at the participant's play history to find the previous track.
        If found, upserts a transition vector in QDrant's ``transitions``
        collection.  The weight field is **incremented** (read-modify-write)
        so that frequently-observed transitions accumulate higher weight.
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

        # Use a deterministic UUID v5 based on the from→to pair.
        transition_id = str(
            uuid5(NAMESPACE_URL, f"{previous_track_id}_{current_track_id}")
        )

        try:
            # Read existing weight (if any).
            existing = await asyncio.to_thread(
                self.qdrant_repo.retrieve_payload,
                _TRANSITIONS_COLLECTION,
                transition_id,
            )
            new_weight = (
                (existing.get("weight", 0) if existing else 0) + 1
            )

            await asyncio.to_thread(
                self.qdrant_repo.upsert,
                _TRANSITIONS_COLLECTION,
                transition_id,
                current_vector,
                {
                    "from_track_id": previous_track_id,
                    "to_track_id": current_track_id,
                    "weight": new_weight,
                },
            )
            logger.info(
                "transition_recorded",
                from_track=previous_track_id,
                to_track=current_track_id,
                weight=new_weight,
            )
        except Exception as exc:
            logger.warning("transition_record_failed", error=str(exc))
