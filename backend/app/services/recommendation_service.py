"""Recommendation service — session-level cluster-based recommendations.

Two strategies:
- ``popular`` (0 tracks played): diverse mix of popular catalog tracks.
- ``cluster`` (1+ tracks): auto-cluster the session's played tracks into
  up to 3 vibe groups, then fill 5 slots (4 cluster + 1 exploration)
  with popularity re-ranking and MMR diversity.

Vector searches use **weighted fusion** of two QDrant collections:
- ``audio_features`` (45-dim librosa, z-score normalised) — weight 0.7
- ``lyrics_embeddings`` (384-dim SentenceTransformer) — weight 0.3
"""

from __future__ import annotations

import asyncio
import math

import numpy as np
import structlog
from karaoke_shared.constants import (
    COLLECTION_AUDIO_FEATURES,
    COLLECTION_LYRICS_EMBEDDINGS,
    PopularityCategory,
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

# Auto-clustering parameters.
_CLUSTER_THRESHOLD = 0.7  # fused cosine similarity threshold
_MAX_CLUSTERS = 3
_SINGLETON_WEIGHT = 0.5  # weight for clusters with only 1 track

# Popularity category weights for re-ranking.
_POPULARITY_WEIGHTS: dict[str, float] = {
    PopularityCategory.ETERNAL_HIT: 1.0,
    PopularityCategory.CURRENT_HIT: 0.7,
    PopularityCategory.ARTIST_BEST: 0.7,
    PopularityCategory.FORMER_HIT: 0.4,
    PopularityCategory.REGULAR: 0.1,
}

# MMR lambda: balance between relevance (1.0) and diversity (0.0).
_MMR_LAMBDA = 0.7


class RecommendedTrack:
    """A track with its similarity score."""

    __slots__ = ("track", "similarity_score")

    def __init__(self, track: Track, similarity_score: float) -> None:
        self.track = track
        self.similarity_score = similarity_score


# ------------------------------------------------------------------
# Session auto-clustering (pure functions, no I/O)
# ------------------------------------------------------------------


def _fused_cosine(
    audio_a: list[float], lyrics_a: list[float],
    audio_b: list[float], lyrics_b: list[float],
) -> float:
    """Compute weighted fused cosine similarity between two tracks."""
    a_a = np.array(audio_a, dtype=np.float64)
    a_b = np.array(audio_b, dtype=np.float64)
    l_a = np.array(lyrics_a, dtype=np.float64)
    l_b = np.array(lyrics_b, dtype=np.float64)

    audio_sim = float(np.dot(a_a, a_b) / (np.linalg.norm(a_a) * np.linalg.norm(a_b) + 1e-9))
    lyrics_sim = float(np.dot(l_a, l_b) / (np.linalg.norm(l_a) * np.linalg.norm(l_b) + 1e-9))

    return _AUDIO_WEIGHT * audio_sim + _LYRICS_WEIGHT * lyrics_sim


def auto_cluster_session(
    track_vectors: list[tuple[str, list[float], list[float]]],
) -> list[dict]:
    """Greedy clustering of session tracks by fused cosine similarity.

    Args:
        track_vectors: list of (track_id, audio_vec, lyrics_vec) tuples.

    Returns:
        List of cluster dicts: {
            "track_ids": [str],
            "centroid_audio": list[float],
            "centroid_lyrics": list[float],
            "weight": float,  # 0.5 for singletons, 1.0 for 2+
        }
    """
    if not track_vectors:
        return []

    clusters: list[dict] = []

    for track_id, audio, lyrics in track_vectors:
        best_sim = -1.0
        best_idx = -1

        for i, cluster in enumerate(clusters):
            sim = _fused_cosine(
                audio, lyrics,
                cluster["centroid_audio"], cluster["centroid_lyrics"],
            )
            if sim > best_sim:
                best_sim = sim
                best_idx = i

        if best_sim >= _CLUSTER_THRESHOLD and best_idx >= 0:
            # Add to existing cluster, recompute centroid (running mean).
            c = clusters[best_idx]
            n = len(c["track_ids"])
            c["track_ids"].append(track_id)
            c["centroid_audio"] = [
                (old * n + new) / (n + 1)
                for old, new in zip(c["centroid_audio"], audio)
            ]
            c["centroid_lyrics"] = [
                (old * n + new) / (n + 1)
                for old, new in zip(c["centroid_lyrics"], lyrics)
            ]
        elif len(clusters) < _MAX_CLUSTERS:
            # Create new cluster.
            clusters.append({
                "track_ids": [track_id],
                "centroid_audio": list(audio),
                "centroid_lyrics": list(lyrics),
            })
        else:
            # Max clusters reached — assign to nearest.
            if best_idx >= 0:
                c = clusters[best_idx]
                n = len(c["track_ids"])
                c["track_ids"].append(track_id)
                c["centroid_audio"] = [
                    (old * n + new) / (n + 1)
                    for old, new in zip(c["centroid_audio"], audio)
                ]
                c["centroid_lyrics"] = [
                    (old * n + new) / (n + 1)
                    for old, new in zip(c["centroid_lyrics"], lyrics)
                ]

    # Assign weights.
    for c in clusters:
        c["weight"] = 1.0 if len(c["track_ids"]) >= 2 else _SINGLETON_WEIGHT

    return clusters


def distribute_slots(clusters: list[dict], total: int) -> list[int]:
    """Distribute N slots proportionally to weighted cluster sizes.

    Each cluster gets at least 1 slot. Returns list of slot counts
    aligned with clusters list.
    """
    if not clusters:
        return []

    weighted = [len(c["track_ids"]) * c["weight"] for c in clusters]
    total_weight = sum(weighted)
    if total_weight == 0:
        return [total // len(clusters)] * len(clusters)

    # Proportional allocation with minimum 1.
    raw = [max(1, round(w / total_weight * total)) for w in weighted]

    # Adjust to exactly total.
    while sum(raw) > total:
        # Remove from largest.
        idx = raw.index(max(raw))
        if raw[idx] > 1:
            raw[idx] -= 1
    while sum(raw) < total:
        # Add to smallest.
        idx = raw.index(min(raw))
        raw[idx] += 1

    return raw


def popularity_rerank(
    candidates: list[RecommendedTrack],
) -> list[RecommendedTrack]:
    """Re-rank candidates by similarity * (1 + popularity_weight)."""
    for c in candidates:
        category = getattr(c.track, "popularity_category", "regular") or "regular"
        weight = _POPULARITY_WEIGHTS.get(category, 0.1)
        c.similarity_score = c.similarity_score * (1.0 + weight)
    candidates.sort(key=lambda c: c.similarity_score, reverse=True)
    return candidates


def mmr_select(
    candidates: list[RecommendedTrack],
    limit: int,
    all_audio_vecs: dict[str, list[float]] | None = None,
    lambda_: float = _MMR_LAMBDA,
) -> list[RecommendedTrack]:
    """Maximal Marginal Relevance selection for diversity.

    If audio vectors are not provided, falls back to simple top-N.
    """
    if not candidates or limit <= 0:
        return []

    if all_audio_vecs is None or len(candidates) <= limit:
        candidates.sort(key=lambda c: c.similarity_score, reverse=True)
        return candidates[:limit]

    selected: list[RecommendedTrack] = []
    remaining = list(candidates)

    # First: best by score.
    remaining.sort(key=lambda c: c.similarity_score, reverse=True)
    selected.append(remaining.pop(0))

    while len(selected) < limit and remaining:
        best_mmr = -float("inf")
        best_idx = 0

        for i, cand in enumerate(remaining):
            cand_vec = all_audio_vecs.get(cand.track.id)
            if cand_vec is None:
                mmr_score = lambda_ * cand.similarity_score
            else:
                max_sim = 0.0
                for sel in selected:
                    sel_vec = all_audio_vecs.get(sel.track.id)
                    if sel_vec is not None:
                        sim = float(np.dot(cand_vec, sel_vec) / (
                            np.linalg.norm(cand_vec) * np.linalg.norm(sel_vec) + 1e-9
                        ))
                        if sim > max_sim:
                            max_sim = sim
                mmr_score = lambda_ * cand.similarity_score - (1 - lambda_) * max_sim

            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = i

        selected.append(remaining.pop(best_idx))

    return selected


# ------------------------------------------------------------------
# Service
# ------------------------------------------------------------------


class RecommendationService:
    """Session-level cluster-based recommendation engine."""

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
        language: str | None = None,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Return recommendations for a session.

        0 tracks played → POPULAR (diverse hits).
        1+ tracks → CLUSTER (auto-clustered session vibes).
        """
        history = await self.sqlite_repo.get_history_by_session(session_id)
        played_ids = {entry.track_id for entry in history}

        if not history:
            return await self._popular_strategy(played_ids, limit)

        return await self._cluster_strategy(history, played_ids, limit, language)

    # ------------------------------------------------------------------
    # Strategies
    # ------------------------------------------------------------------

    async def _popular_strategy(
        self, played_ids: set[str], limit: int
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Return a mix of popular and random tracks."""
        n_top = max(1, int(limit * 0.7))
        n_random = limit - n_top

        extra = len(played_ids) + limit
        top_tracks = await self.sqlite_repo.list_popular(limit=n_top + extra)
        random_tracks = await self.sqlite_repo.list_random(limit=n_random + extra)

        seen: set[str] = set(played_ids)
        results: list[RecommendedTrack] = []

        for t in top_tracks:
            if t.id not in seen:
                seen.add(t.id)
                results.append(RecommendedTrack(track=t, similarity_score=0.0))
                if len(results) >= n_top:
                    break

        for t in random_tracks:
            if t.id not in seen:
                seen.add(t.id)
                results.append(RecommendedTrack(track=t, similarity_score=0.0))
                if len(results) >= limit:
                    break

        return RecommendationStrategy.POPULAR, results

    async def _cluster_strategy(
        self,
        history: list,
        played_ids: set[str],
        limit: int,
        language: str | None = None,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Cluster-based recommendations from session play history."""
        # 1. Fetch vectors for played tracks.
        track_ids = [entry.track_id for entry in history]
        vectors = await self._fetch_track_vectors(track_ids)

        if not vectors:
            return await self._popular_strategy(played_ids, limit)

        # 2. Auto-cluster session.
        clusters = auto_cluster_session(vectors)
        if not clusters:
            return await self._popular_strategy(played_ids, limit)

        # 3. Distribute slots: (limit - 1) for clusters, 1 for exploration.
        cluster_slots = limit - 1 if limit > 1 else limit
        slot_counts = distribute_slots(clusters, cluster_slots)

        # 4. KNN per cluster centroid → candidates with popularity re-ranking.
        all_candidates: list[RecommendedTrack] = []
        audio_vecs: dict[str, list[float]] = {}
        knn_filters = {"status": "ready"}
        if language:
            knn_filters["language"] = language

        for cluster, n_slots in zip(clusters, slot_counts):
            candidates = await self._fused_knn_search(
                cluster["centroid_audio"],
                cluster["centroid_lyrics"],
                played_ids,
                n_slots * 4,  # oversample for re-ranking
                knn_filters,
            )
            candidates = popularity_rerank(candidates)
            all_candidates.extend(candidates[:n_slots * 2])

            # Collect audio vectors for MMR.
            for c in candidates:
                vec = await self._get_track_vector(c.track.id)
                if vec:
                    audio_vecs[c.track.id] = vec

        # 5. Exploration: popular track far from all cluster centroids.
        if limit > 1:
            exploration = await self._exploration_track(
                clusters, played_ids | {c.track.id for c in all_candidates},
                language,
            )
            if exploration:
                all_candidates.append(exploration)
                vec = await self._get_track_vector(exploration.track.id)
                if vec:
                    audio_vecs[exploration.track.id] = vec

        # 6. MMR selection across all candidates.
        final = mmr_select(all_candidates, limit, audio_vecs)

        return RecommendationStrategy.CLUSTER, final

    async def _exploration_track(
        self,
        clusters: list[dict],
        exclude_ids: set[str],
        language: str | None = None,
    ) -> RecommendedTrack | None:
        """Find a popular track maximally distant from all cluster centroids."""
        extra = len(exclude_ids) + 20
        top_tracks = await self.sqlite_repo.list_popular(limit=extra)

        best_track: Track | None = None
        best_min_sim = float("inf")

        for track in top_tracks:
            if track.id in exclude_ids:
                continue
            if language and track.language != language:
                continue

            vec = await self._get_track_vector(track.id)
            if vec is None:
                continue

            # Min similarity to any cluster centroid.
            min_sim = float("inf")
            for cluster in clusters:
                sim = float(np.dot(vec, cluster["centroid_audio"]) / (
                    np.linalg.norm(vec) * np.linalg.norm(cluster["centroid_audio"]) + 1e-9
                ))
                if sim < min_sim:
                    min_sim = sim

            if min_sim < best_min_sim:
                best_min_sim = min_sim
                best_track = track

        if best_track is None:
            return None

        return RecommendedTrack(track=best_track, similarity_score=0.5)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fetch_track_vectors(
        self, track_ids: list[str]
    ) -> list[tuple[str, list[float], list[float]]]:
        """Fetch audio + lyrics vectors for a list of tracks. Skips missing."""
        results: list[tuple[str, list[float], list[float]]] = []
        # Fetch in parallel batches.
        tasks = [
            asyncio.gather(
                self._get_track_vector(tid),
                self._get_track_lyrics_vector(tid),
            )
            for tid in track_ids
        ]
        vectors = await asyncio.gather(*tasks)

        for tid, (audio, lyrics) in zip(track_ids, vectors):
            if audio is not None and lyrics is not None:
                results.append((tid, audio, lyrics))

        return results

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
        filters: dict | None = None,
    ) -> list[RecommendedTrack]:
        """Run weighted fusion KNN across audio and lyrics collections."""
        if filters is None:
            filters = {"status": "ready"}

        oversample = limit + len(exclude_ids) + 10

        audio_task = self._knn_raw(
            _AUDIO_COLLECTION, audio_vector, oversample, filters
        ) if audio_vector else _empty_coro()

        lyrics_task = self._knn_raw(
            _LYRICS_COLLECTION, lyrics_vector, oversample, filters
        ) if lyrics_vector else _empty_coro()

        audio_hits, lyrics_hits = await asyncio.gather(audio_task, lyrics_task)

        audio_scores: dict[str, float] = {
            pid: score for pid, score, _ in audio_hits
            if pid not in exclude_ids
        }
        lyrics_scores: dict[str, float] = {
            pid: score for pid, score, _ in lyrics_hits
            if pid not in exclude_ids
        }

        all_ids = set(audio_scores) | set(lyrics_scores)
        if not all_ids:
            return []

        if audio_scores and lyrics_scores:
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
            fused = [(pid, score) for pid, score in audio_scores.items()]
        else:
            fused = [(pid, score) for pid, score in lyrics_scores.items()]

        fused.sort(key=lambda x: x[1], reverse=True)
        top = fused[:limit]

        if not top:
            return []

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
        filters: dict | None = None,
    ) -> list[tuple[str, float, dict]]:
        """Run raw KNN search in a single QDrant collection."""
        if filters is None:
            filters = {"status": "ready"}
        try:
            return await asyncio.to_thread(
                self.qdrant_repo.search,
                collection,
                vector,
                limit=limit,
                filters=filters,
            )
        except Exception as exc:
            logger.warning("knn_search_failed", collection=collection, error=str(exc))
            return []


async def _empty_coro() -> list:
    """Return an empty list — used as a no-op coroutine for asyncio.gather."""
    return []
