"""Recommendation service — content-based + collaborative track recommendations.

Strategies are selected automatically based on how many tracks the
participant has already sung in the current session:

- 0 tracks → ``popular``: 70% most-played + 30% random from catalog.
- 1 track  → ``last``: transition graph candidates + fused KNN on last track.
- 2 tracks → ``last_two_avg``: fused KNN on the average of the last two vectors.
- 3+ tracks → ``session_avg``: fused KNN on the participant's EMA portrait vectors.

Vector searches use **weighted fusion** of two QDrant collections:
- ``audio_features`` (45-dim librosa, z-score normalised) — weight 0.7
- ``lyrics_embeddings`` (384-dim SentenceTransformer) — weight 0.3

When a track has no lyrics vector, the system falls back to pure audio KNN.

Portrait vectors are updated using Exponential Moving Average (EMA) with
``alpha = 0.3`` and L2-renormalised after each update.  Both audio and
lyrics portraits are maintained independently.
"""

from __future__ import annotations

import asyncio
from uuid import NAMESPACE_URL, uuid5

import numpy as np
import structlog
from karaoke_shared.constants import (
    COLLECTION_AUDIO_FEATURES,
    COLLECTION_LYRICS_EMBEDDINGS,
    COLLECTION_TRANSITIONS,
)
from karaoke_shared.models.recommendation import RecommendationStrategy
from karaoke_shared.models.track import Track
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

logger = structlog.get_logger(__name__)

_AUDIO_COLLECTION = COLLECTION_AUDIO_FEATURES
_LYRICS_COLLECTION = COLLECTION_LYRICS_EMBEDDINGS
_TRANSITIONS_COLLECTION = COLLECTION_TRANSITIONS

# Weight of the most recent track in EMA portrait update.
# Effective window ≈ 1/alpha ≈ 3-4 most recent tracks.
_EMA_ALPHA = 0.3

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
            if len(history) < 1:
                return await self._popular_strategy(played_ids, limit)
            return await self._last_strategy(history[0].track_id, played_ids, limit)

        if tracks_played == 2:
            if len(history) < 2:
                if len(history) == 1:
                    return await self._last_strategy(
                        history[0].track_id, played_ids, limit
                    )
                return await self._popular_strategy(played_ids, limit)
            return await self._last_two_avg_strategy(
                history[0].track_id, history[1].track_id, played_ids, limit
            )

        # 3+ tracks: use the participant's EMA portrait vectors.
        if participant and participant.portrait_vector:
            return await self._session_avg_strategy(
                participant.portrait_vector,
                participant.lyrics_portrait_vector,
                played_ids,
                limit,
            )

        # Fallback: portrait missing — cascade to a strategy the history can support.
        if len(history) >= 2:
            return await self._last_two_avg_strategy(
                history[0].track_id, history[1].track_id, played_ids, limit
            )
        if len(history) == 1:
            return await self._last_strategy(
                history[0].track_id, played_ids, limit
            )
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

    async def _last_strategy(
        self, track_id: str, played_ids: set[str], limit: int
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Transition graph candidates + fused KNN on last track.

        If the transition graph has data for this track, those candidates
        come first (real human signal). Remaining slots are filled by
        fused audio+lyrics KNN.
        """
        # Transition candidates (up to half the slots).
        trans_limit = limit // 2
        trans_ids = await self._transition_candidates(track_id, played_ids, trans_limit)

        # Fetch both vectors for the track.
        audio_vec = await self._get_track_vector(track_id)
        lyrics_vec = await self._get_track_lyrics_vector(track_id)

        if audio_vec is None and lyrics_vec is None and not trans_ids:
            return await self._popular_strategy(played_ids, limit)

        knn_results: list[RecommendedTrack] = []
        if audio_vec is not None or lyrics_vec is not None:
            knn_exclude = played_ids | set(trans_ids)
            knn_results = await self._fused_knn_search(
                audio_vec, lyrics_vec, knn_exclude, limit - len(trans_ids)
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
        """Fused KNN on the L2-normalised average of the two most recent vectors."""
        a1 = await self._get_track_vector(track_id_1)
        a2 = await self._get_track_vector(track_id_2)
        l1 = await self._get_track_lyrics_vector(track_id_1)
        l2 = await self._get_track_lyrics_vector(track_id_2)

        audio_avg = _avg_vectors(a1, a2)
        lyrics_avg = _avg_vectors(l1, l2)

        if audio_avg is None and lyrics_avg is None:
            return await self._popular_strategy(played_ids, limit)

        results = await self._fused_knn_search(
            audio_avg, lyrics_avg, played_ids, limit
        )
        return RecommendationStrategy.LAST_TWO_AVG, results

    async def _session_avg_strategy(
        self,
        portrait_vector: list[float],
        lyrics_portrait_vector: list[float] | None,
        played_ids: set[str],
        limit: int,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Fused KNN on the participant's session EMA portrait vectors."""
        results = await self._fused_knn_search(
            portrait_vector, lyrics_portrait_vector, played_ids, limit
        )
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
            # Both available — weighted fusion.
            fused: list[tuple[str, float]] = []
            for pid in all_ids:
                a = audio_scores.get(pid, 0.0)
                l = lyrics_scores.get(pid, 0.0)
                fused.append((pid, _AUDIO_WEIGHT * a + _LYRICS_WEIGHT * l))
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
        """Update the participant's portrait vectors after finishing a track.

        Uses Exponential Moving Average (EMA) for both audio and lyrics
        portrait vectors independently.  Each is L2-renormalised.

        Returns the updated audio portrait vector, or ``None`` if the
        track has no audio vector in QDrant.
        """
        audio_vec = await self._get_track_vector(track_id)
        lyrics_vec = await self._get_track_lyrics_vector(track_id)

        if audio_vec is None:
            return None

        participant = await self.sqlite_repo.get_participant(participant_id)
        if participant is None:
            return None

        n = participant.tracks_played  # Already incremented by finish_playing.

        # Audio portrait EMA.
        new_audio = _ema_update(participant.portrait_vector, audio_vec, n)

        # Lyrics portrait EMA — only update if the current track has lyrics.
        # When lyrics_vec is None (instrumental / no transcription), we
        # preserve the previously accumulated lyrics portrait as-is.
        new_lyrics: list[float] | None = None
        if lyrics_vec is not None:
            new_lyrics = _ema_update(
                participant.lyrics_portrait_vector, lyrics_vec, n
            )

        await self.sqlite_repo.update_portrait(
            participant_id, new_audio, new_lyrics
        )
        return new_audio

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


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _ema_update(
    old_portrait: list[float] | None,
    current_vector: list[float],
    n: int,
) -> list[float]:
    """Apply EMA update to a portrait vector and L2-renormalise."""
    if old_portrait is None or n <= 1:
        new = current_vector
    else:
        new = [
            _EMA_ALPHA * cur + (1 - _EMA_ALPHA) * old
            for old, cur in zip(old_portrait, current_vector)
        ]

    arr = np.array(new, dtype=np.float64)
    norm = np.linalg.norm(arr)
    if norm > 1e-8:
        arr = arr / norm
    return arr.tolist()


def _avg_vectors(
    v1: list[float] | None, v2: list[float] | None
) -> list[float] | None:
    """Average two vectors with L2-renormalisation.  Returns None if both are None."""
    if v1 is None and v2 is None:
        return None
    if v1 is None:
        return v2
    if v2 is None:
        return v1

    avg = np.array([(a + b) / 2.0 for a, b in zip(v1, v2)], dtype=np.float64)
    norm = np.linalg.norm(avg)
    if norm > 1e-8:
        avg = avg / norm
    return avg.tolist()


async def _empty_coro() -> list:
    """Return an empty list — used as a no-op coroutine for asyncio.gather."""
    return []
