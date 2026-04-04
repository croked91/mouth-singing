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

import numpy as np
import structlog
from karaoke_shared.constants import (
    COLLECTION_AUDIO_FEATURES,
    COLLECTION_LYRICS_EMBEDDINGS,
    PopularityCategory,
    WELL_KNOWN_CATEGORIES,
)
from karaoke_shared.models.recommendation import RecommendationStrategy
from karaoke_shared.models.track import Track
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

logger = structlog.get_logger(__name__)

_AUDIO_COLLECTION = COLLECTION_AUDIO_FEATURES
_LYRICS_COLLECTION = COLLECTION_LYRICS_EMBEDDINGS

# Fusion weights for audio and lyrics KNN scores.
# Audio dominates for KNN — librosa features find same-genre tracks better
# than lyrics embeddings which match by word semantics (not music style).
# Note: clustering uses inverted weights (audio 30%, lyrics 70%) because
# K-Means on 25k tracks benefits from thematic grouping.
_AUDIO_WEIGHT = 0.7
_LYRICS_WEIGHT = 0.3

# Auto-clustering parameters.
_CLUSTER_THRESHOLD = 0.5  # fused cosine similarity threshold (lowered to separate genres)
_MAX_CLUSTERS = 5
_SINGLETON_WEIGHT = 1.0  # all clusters weighted equally regardless of size

# Popularity category weights for re-ranking.
# Formula: final_score = similarity * (1 + weight).
# Higher weights make hits float above regular tracks.
_POPULARITY_WEIGHTS: dict[str, float] = {
    PopularityCategory.ETERNAL_HIT: 2.0,
    PopularityCategory.CURRENT_HIT: 1.5,
    PopularityCategory.ARTIST_BEST: 1.2,
    PopularityCategory.FORMER_HIT: 0.6,
    PopularityCategory.REGULAR: 0.0,
}

# MMR lambda: balance between relevance (1.0) and diversity (0.0).
_MMR_LAMBDA = 0.7

# Hit priority: only hits with fused score >= threshold get sorted to top.
# Below this, a hit is ranked by score like any regular track.
_HIT_SCORE_THRESHOLD = 0.5
_HIT_CATEGORIES = {
    PopularityCategory.ETERNAL_HIT,
    PopularityCategory.CURRENT_HIT,
    PopularityCategory.ARTIST_BEST,
}


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


def _update_cluster_centroid(
    cluster: dict, audio: list[float], lyrics: list[float],
) -> None:
    """Add a track to a cluster and recompute its centroid (running mean)."""
    n = len(cluster["track_ids"]) - 1  # track_id already appended by caller
    cluster["centroid_audio"] = [
        (old * n + new) / (n + 1)
        for old, new in zip(cluster["centroid_audio"], audio)
    ]
    cluster["centroid_lyrics"] = [
        (old * n + new) / (n + 1)
        for old, new in zip(cluster["centroid_lyrics"], lyrics)
    ]


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
            c["track_ids"].append(track_id)
            _update_cluster_centroid(c, audio, lyrics)
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
                c["track_ids"].append(track_id)
                _update_cluster_centroid(c, audio, lyrics)

    # Assign weights.
    for c in clusters:
        c["weight"] = 1.0 if len(c["track_ids"]) >= 2 else _SINGLETON_WEIGHT

    return clusters


def distribute_slots(clusters: list[dict], total: int) -> list[int]:
    """Distribute N slots equally across clusters.

    Each cluster gets the same number of slots. Remainder distributed
    round-robin. This ensures every vibe gets equal representation.
    """
    if not clusters:
        return []

    n = len(clusters)
    if total < n:
        raw = [0] * n
        for i in range(total):
            raw[i] = 1
        return raw

    base = total // n
    remainder = total % n
    raw = [base] * n
    for i in range(remainder):
        raw[i] += 1
    while sum(raw) < total:
        # Add to smallest.
        idx = raw.index(min(raw))
        raw[idx] += 1

    return raw


def popularity_rerank(
    candidates: list[RecommendedTrack],
) -> list[RecommendedTrack]:
    """Re-rank candidates by similarity * (1 + popularity_weight).

    Returns new RecommendedTrack objects — originals are not mutated.
    """
    reranked = []
    for c in candidates:
        category = getattr(c.track, "popularity_category", "regular") or "regular"
        weight = _POPULARITY_WEIGHTS.get(category, 0.1)
        reranked.append(
            RecommendedTrack(
                track=c.track,
                similarity_score=c.similarity_score * (1.0 + weight),
            )
        )
    reranked.sort(key=lambda c: c.similarity_score, reverse=True)
    return reranked


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


def hit_priority_sort(
    candidates: list[RecommendedTrack],
) -> list[RecommendedTrack]:
    """Sort candidates: hits with fusion score >= threshold first, rest by score.

    A hit (eternal_hit / current_hit / artist_best) is only prioritised
    when its similarity score is high enough (>= 0.6).  This prevents
    a distant hit from being forced above a closely-matching regular track.
    """
    priority: list[RecommendedTrack] = []
    regular: list[RecommendedTrack] = []
    for c in candidates:
        cat = getattr(c.track, "popularity_category", "regular") or "regular"
        if cat in _HIT_CATEGORIES and c.similarity_score >= _HIT_SCORE_THRESHOLD:
            priority.append(c)
        else:
            regular.append(c)
    priority.sort(key=lambda c: c.similarity_score, reverse=True)
    regular.sort(key=lambda c: c.similarity_score, reverse=True)
    return priority + regular


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
        extra_exclude_ids: set[str] | None = None,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Return recommendations for a session.

        0 tracks played → POPULAR (diverse hits).
        1+ tracks → CLUSTER (auto-clustered session vibes).
        """
        history = await self.sqlite_repo.get_history_by_session(session_id)
        played_ids = {entry.track_id for entry in history}

        # Store extra excludes for _cluster_strategy to use.
        self._extra_exclude = extra_exclude_ids or set()

        # Merge with extra excludes (previously shown tracks).
        exclude_ids = played_ids | self._extra_exclude

        # Collect played artists to avoid recommending the same artist again.
        played_artists: set[str] = set()
        if played_ids:
            tracks_map = await self.sqlite_repo.get_tracks_by_ids(list(played_ids))
            played_artists = {t.artist for t in tracks_map.values()}

        if not history:
            return await self._popular_strategy(exclude_ids, limit, language)

        return await self._cluster_strategy(
            history, exclude_ids, played_artists, limit, language,
        )

    async def get_tag_recommendations(
        self,
        tag_centroid_audio: list[float],
        tag_centroid_lyrics: list[float],
        session_id: str,
        limit: int = 5,
        language: str | None = None,
        tag_cluster_id: int | None = None,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Return recommendations for a specific mood-tag cluster.

        When the session has play history, uses the session centroid
        (average of played tracks) as query vector and filters by the
        tag's cluster.  This personalises tag results to the user's taste.

        When there is no history, falls back to the tag cluster centroid
        without a cluster filter (original behaviour).
        """
        history = await self.sqlite_repo.get_history_by_session(session_id)
        played_ids = {entry.track_id for entry in history}

        filters: dict = {"status": "ready"}
        if language:
            filters["language"] = language

        query_audio = tag_centroid_audio
        query_lyrics = tag_centroid_lyrics

        # Personalise: use session centroid + cluster filter when possible.
        if history and tag_cluster_id is not None:
            all_played_ids = list(played_ids)
            centroids = await self._compute_session_centroids({0: all_played_ids})
            if centroids:
                query_audio, query_lyrics = centroids[0]
                filters["rec_cluster_id"] = tag_cluster_id

        results = await self._fused_knn_search(
            query_audio, query_lyrics, played_ids, limit, filters,
        )
        results = hit_priority_sort(results)
        return RecommendationStrategy.CLUSTER, results

    # ------------------------------------------------------------------
    # Strategies
    # ------------------------------------------------------------------

    async def _popular_strategy(
        self, played_ids: set[str], limit: int, language: str | None = None
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """Return a mix of popular and random tracks."""
        n_top = max(1, int(limit * 0.7))
        n_random = limit - n_top

        extra = len(played_ids) + limit
        top_tracks = await self.sqlite_repo.list_popular(limit=n_top + extra, categories=WELL_KNOWN_CATEGORIES)
        random_tracks = await self.sqlite_repo.list_random(limit=n_random + extra, categories=WELL_KNOWN_CATEGORIES)

        seen: set[str] = set(played_ids)
        results: list[RecommendedTrack] = []

        for t in top_tracks:
            if t.id not in seen and (not language or t.language == language):
                seen.add(t.id)
                results.append(RecommendedTrack(track=t, similarity_score=0.0))
                if len(results) >= n_top:
                    break

        for t in random_tracks:
            if t.id not in seen and (not language or t.language == language):
                seen.add(t.id)
                results.append(RecommendedTrack(track=t, similarity_score=0.0))
                if len(results) >= limit:
                    break

        return RecommendationStrategy.POPULAR, results

    async def _cluster_strategy(
        self,
        history: list,
        played_ids: set[str],
        played_artists: set[str],
        limit: int,
        language: str | None = None,
    ) -> tuple[RecommendationStrategy, list[RecommendedTrack]]:
        """KNN-within-cluster recommendations from session play history.

        Groups played tracks by rec_cluster_id, computes a centroid per
        group, then runs fused KNN in QDrant filtered to each cluster.
        Hits with fusion score >= 0.6 are prioritised; the rest are
        ranked by pure similarity.
        """
        # 1. Get rec_cluster_id for each played track.
        unique_ids = list({entry.track_id for entry in history})
        tracks_map = await self.sqlite_repo.get_tracks_by_ids(unique_ids)

        # Group played track IDs by rec_cluster_id.
        cluster_track_ids: dict[int, list[str]] = {}
        for tid in unique_ids:
            track = tracks_map.get(tid)
            if track and track.rec_cluster_id is not None:
                cluster_track_ids.setdefault(track.rec_cluster_id, []).append(tid)

        if not cluster_track_ids:
            return await self._popular_strategy(played_ids, limit, language)

        # 2. Compute centroid per cluster from played track vectors.
        centroids = await self._compute_session_centroids(cluster_track_ids)
        if not centroids:
            return await self._popular_strategy(played_ids, limit, language)

        cluster_ids = list(centroids.keys())

        logger.info(
            "catalog_clusters",
            n_clusters=len(cluster_ids),
            cluster_ids=cluster_ids,
        )

        # 3. Distribute all slots equally across clusters.
        slot_counts = distribute_slots(
            [{"track_ids": cluster_track_ids.get(cid, [])} for cid in cluster_ids],
            limit,
        )

        logger.info("slot_distribution", slot_counts=slot_counts)

        # 4. KNN within each cluster + hit priority + artist dedup.
        all_candidates: list[RecommendedTrack] = []
        exclude_all = played_ids | (self._extra_exclude or set())
        global_seen_artists: set[str] = set(played_artists)

        for cid, n_slots in zip(cluster_ids, slot_counts):
            if n_slots == 0:
                continue

            audio_centroid, lyrics_centroid = centroids[cid]
            filters: dict = {"status": "ready", "rec_cluster_id": cid}
            if language:
                filters["language"] = language

            knn_results = await self._fused_knn_search(
                audio_centroid, lyrics_centroid, exclude_all, n_slots * 3, filters,
            )

            # Hit priority sort.
            sorted_results = hit_priority_sort(knn_results)

            # Artist dedup: 1 artist per cluster, global across clusters.
            cluster_picks: list[RecommendedTrack] = []
            for r in sorted_results:
                if r.track.artist not in global_seen_artists:
                    global_seen_artists.add(r.track.artist)
                    cluster_picks.append(r)
                    if len(cluster_picks) >= n_slots:
                        break

            logger.info(
                "cluster_result",
                cluster_id=cid,
                n_slots=n_slots,
                n_picked=len(cluster_picks),
                top3=[(t.track.artist, t.track.title) for t in cluster_picks[:3]],
            )
            all_candidates.extend(cluster_picks)

        return RecommendationStrategy.CLUSTER, all_candidates[:limit]

    async def _exploration_track(
        self,
        clusters: list[dict],
        exclude_ids: set[str],
        language: str | None = None,
    ) -> RecommendedTrack | None:
        """Find a popular track maximally distant from all cluster centroids."""
        extra = len(exclude_ids) + 20
        top_tracks = await self.sqlite_repo.list_popular(limit=extra, categories=WELL_KNOWN_CATEGORIES)

        # Filter eligible tracks.
        eligible = [
            t for t in top_tracks
            if t.id not in exclude_ids and (not language or t.language == language)
        ]

        # Batch-fetch audio + lyrics vectors for all eligible tracks.
        audio_vecs_list = await asyncio.gather(
            *(self._get_track_vector(t.id) for t in eligible)
        )
        lyrics_vecs_list = await asyncio.gather(
            *(self._get_track_lyrics_vector(t.id) for t in eligible)
        )
        track_vecs = [
            (t, audio_vec, lyrics_vec)
            for t, audio_vec, lyrics_vec in zip(eligible, audio_vecs_list, lyrics_vecs_list)
            if audio_vec is not None
        ]

        best_track: Track | None = None
        best_max_sim = float("inf")

        for track, audio_vec, lyrics_vec in track_vecs:
            # Max similarity to the nearest (closest) cluster centroid.
            max_sim = -float("inf")
            for cluster in clusters:
                sim = _fused_cosine(
                    audio_vec, lyrics_vec if lyrics_vec else [],
                    cluster["centroid_audio"], cluster["centroid_lyrics"],
                )
                if sim > max_sim:
                    max_sim = sim

            if max_sim < best_max_sim:
                best_max_sim = max_sim
                best_track = track

        if best_track is None:
            return None

        # Apply popularity weight to the exploration score.
        base_score = 0.5
        category = getattr(best_track, "popularity_category", "regular") or "regular"
        weight = _POPULARITY_WEIGHTS.get(category, 0.1)
        exploration_score = base_score * (1.0 + weight)

        return RecommendedTrack(track=best_track, similarity_score=exploration_score)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _compute_session_centroids(
        self,
        track_ids_by_cluster: dict[int, list[str]],
    ) -> dict[int, tuple[list[float], list[float]]]:
        """Compute audio+lyrics centroid per cluster from played track vectors.

        Returns a dict mapping cluster_id → (audio_centroid, lyrics_centroid).
        Clusters whose tracks have no vectors in QDrant are omitted.
        """
        all_ids = [tid for ids in track_ids_by_cluster.values() for tid in ids]
        track_vectors = await self._fetch_track_vectors(all_ids)
        vec_map = {tid: (audio, lyrics) for tid, audio, lyrics in track_vectors}

        centroids: dict[int, tuple[list[float], list[float]]] = {}
        for cid, tids in track_ids_by_cluster.items():
            vecs = [vec_map[tid] for tid in tids if tid in vec_map]
            if not vecs:
                continue
            audio_centroid = np.mean([v[0] for v in vecs], axis=0).tolist()
            lyrics_centroid = np.mean([v[1] for v in vecs], axis=0).tolist()
            centroids[cid] = (audio_centroid, lyrics_centroid)
        return centroids

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
        allowed_categories: set[str] | None = None,
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
                a = audio_scores.get(pid, 0.0)
                l = lyrics_scores.get(pid, 0.0)
                # No normalization — missing collection = 0, not "ignored".
                # This ensures tracks must score well in BOTH collections.
                fused.append(
                    (pid, _AUDIO_WEIGHT * a + _LYRICS_WEIGHT * l)
                )
        elif audio_scores:
            fused = [(pid, score * _AUDIO_WEIGHT) for pid, score in audio_scores.items()]
        else:
            fused = [(pid, score * _LYRICS_WEIGHT) for pid, score in lyrics_scores.items()]

        fused.sort(key=lambda x: x[1], reverse=True)
        top = fused[:limit]

        if not top:
            return []

        candidate_ids = [pid for pid, _ in top]
        tracks_map = await self.sqlite_repo.get_tracks_by_ids(candidate_ids)

        # Split into well-known and regular, prefer well-known.
        well_known: list[RecommendedTrack] = []
        regular: list[RecommendedTrack] = []
        for pid, score in top:
            track = tracks_map.get(pid)
            if track is not None:
                rt = RecommendedTrack(track=track, similarity_score=score)
                if allowed_categories and (track.popularity_category or "regular") not in allowed_categories:
                    regular.append(rt)
                else:
                    well_known.append(rt)

        # Fill with well-known first, then regular if not enough.
        results = well_known[:limit]
        if len(results) < limit:
            results.extend(regular[:limit - len(results)])

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
