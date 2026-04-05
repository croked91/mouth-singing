"""Recommendation engine — KNN-within-cluster with hit priority.

Ported from backend/app/services/recommendation_service.py.
Key difference: all track metadata comes from QDrant payloads,
not from PostgreSQL. Catalog clusters and mood tags are loaded
from an in-memory JSON file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import numpy as np
import structlog
from karaoke_shared.constants import (
    COLLECTION_AUDIO_FEATURES,
    COLLECTION_LYRICS_EMBEDDINGS,
    PopularityCategory,
)
from karaoke_shared.repositories.qdrant_repository import QDrantRepository

logger = structlog.get_logger(__name__)

_AUDIO_COLLECTION = COLLECTION_AUDIO_FEATURES
_LYRICS_COLLECTION = COLLECTION_LYRICS_EMBEDDINGS

_AUDIO_WEIGHT = 0.7
_LYRICS_WEIGHT = 0.3

_HIT_SCORE_THRESHOLD = 0.5
_HIT_CATEGORIES = {
    PopularityCategory.ETERNAL_HIT,
    PopularityCategory.CURRENT_HIT,
    PopularityCategory.ARTIST_BEST,
}


@dataclass(slots=True)
class RecTrack:
    """Lightweight track built from QDrant payload."""

    id: str
    artist: str
    title: str
    duration_sec: int | None
    language: str | None
    popularity_category: str
    rec_cluster_id: int | None
    catalog_cluster_id: int | None


class RecommendedTrack:
    __slots__ = ("track", "similarity_score")

    def __init__(self, track: RecTrack, similarity_score: float) -> None:
        self.track = track
        self.similarity_score = similarity_score


# ------------------------------------------------------------------
# Pure functions
# ------------------------------------------------------------------


def distribute_slots(clusters: list[dict], total: int) -> list[int]:
    """Distribute N slots equally across clusters."""
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
    return raw


def hit_priority_sort(candidates: list[RecommendedTrack]) -> list[RecommendedTrack]:
    """Hits with score >= threshold first, rest by score."""
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


def _track_from_payload(point_id: str, payload: dict) -> RecTrack:
    """Build a RecTrack from a QDrant point payload."""
    return RecTrack(
        id=payload.get("track_id", point_id),
        artist=payload.get("artist", ""),
        title=payload.get("title", ""),
        duration_sec=payload.get("duration_sec"),
        language=payload.get("language"),
        popularity_category=payload.get("popularity_category", "regular"),
        rec_cluster_id=payload.get("rec_cluster_id"),
        catalog_cluster_id=payload.get("catalog_cluster_id"),
    )


# ------------------------------------------------------------------
# Engine
# ------------------------------------------------------------------


class RecommendationEngine:
    """KNN-within-cluster recommendation engine.

    Uses only QDrant + in-memory catalog data. No PostgreSQL.
    """

    def __init__(
        self,
        qdrant_repo: QDrantRepository,
        catalog_data: dict,
    ) -> None:
        self.qdrant_repo = qdrant_repo
        self._clusters = {c["id"]: c for c in catalog_data.get("clusters", [])}
        self._tags = catalog_data.get("tags", [])

    async def get_recommendations(
        self,
        played_track_ids: list[str],
        limit: int = 5,
        language: str | None = None,
        exclude_ids: set[str] | None = None,
    ) -> tuple[str, list[RecommendedTrack]]:
        """Return cluster KNN recommendations based on played tracks.

        Returns ("cluster", results). Caller should handle the case
        when played_track_ids is empty (popular strategy).
        """
        exclude_ids = exclude_ids or set()
        played_set = set(played_track_ids)
        all_exclude = played_set | exclude_ids

        # 1. Get rec_cluster_id for each played track from QDrant payload.
        cluster_track_ids: dict[int, list[str]] = {}
        played_artists: set[str] = set()

        for tid in played_track_ids:
            payload = await asyncio.to_thread(
                self.qdrant_repo.retrieve_payload, _AUDIO_COLLECTION, tid
            )
            if payload is None:
                continue
            played_artists.add(payload.get("artist", ""))
            cid = payload.get("rec_cluster_id")
            if cid is not None:
                cluster_track_ids.setdefault(cid, []).append(tid)

        if not cluster_track_ids:
            return "cluster", []

        # 2. Compute centroid per cluster.
        centroids = await self._compute_session_centroids(cluster_track_ids)
        if not centroids:
            return "cluster", []

        cluster_ids = list(centroids.keys())

        logger.info("catalog_clusters", n_clusters=len(cluster_ids), cluster_ids=cluster_ids)

        # 3. Distribute slots equally.
        slot_counts = distribute_slots(
            [{"track_ids": cluster_track_ids.get(cid, [])} for cid in cluster_ids],
            limit,
        )

        # 4. KNN per cluster + hit priority + artist dedup.
        all_candidates: list[RecommendedTrack] = []
        global_seen_artists: set[str] = set(played_artists)

        for cid, n_slots in zip(cluster_ids, slot_counts):
            if n_slots == 0:
                continue

            audio_centroid, lyrics_centroid = centroids[cid]
            filters: dict = {"status": "ready", "rec_cluster_id": cid}
            if language:
                filters["language"] = language

            knn_results = await self._fused_knn_search(
                audio_centroid, lyrics_centroid, all_exclude, n_slots * 3, filters,
            )

            sorted_results = hit_priority_sort(knn_results)

            cluster_picks: list[RecommendedTrack] = []
            for r in sorted_results:
                if r.track.artist not in global_seen_artists:
                    global_seen_artists.add(r.track.artist)
                    cluster_picks.append(r)
                    if len(cluster_picks) >= n_slots:
                        break

            logger.info(
                "cluster_result", cluster_id=cid, n_slots=n_slots,
                n_picked=len(cluster_picks),
                top3=[(t.track.artist, t.track.title) for t in cluster_picks[:3]],
            )
            all_candidates.extend(cluster_picks)

        return "cluster", all_candidates[:limit]

    async def get_tag_recommendations(
        self,
        tag_id: int,
        played_track_ids: list[str],
        limit: int = 5,
        language: str | None = None,
    ) -> tuple[str, list[RecommendedTrack]]:
        """Return tag-based KNN recommendations, personalised to session."""
        # Find the tag and its cluster.
        tag = next((t for t in self._tags if t["id"] == tag_id), None)
        if tag is None:
            return "cluster", []

        cluster = self._clusters.get(tag["cluster_id"])
        if cluster is None:
            return "cluster", []

        played_set = set(played_track_ids)
        filters: dict = {"status": "ready"}
        if language:
            filters["language"] = language

        query_audio = cluster["centroid_audio"]
        query_lyrics = cluster["centroid_lyrics"]

        # Personalise: use session centroid + cluster filter.
        if played_track_ids:
            centroids = await self._compute_session_centroids({0: played_track_ids})
            if centroids:
                query_audio, query_lyrics = centroids[0]
                filters["rec_cluster_id"] = tag["cluster_id"]

        results = await self._fused_knn_search(
            query_audio, query_lyrics, played_set, limit, filters,
        )
        results = hit_priority_sort(results)
        return "cluster", results

    def get_tags(
        self,
        played_track_payloads: list[dict],
        limit: int = 8,
    ) -> list[dict]:
        """Return mood tags excluding clusters covered by played tracks."""
        import random

        covered_clusters: set[int] = set()
        for payload in played_track_payloads:
            cid = payload.get("catalog_cluster_id")
            if cid is not None:
                covered_clusters.add(cid)

        available = [t for t in self._tags if t["cluster_id"] not in covered_clusters]
        if len(available) <= limit:
            return available
        return random.sample(available, limit)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _compute_session_centroids(
        self, track_ids_by_cluster: dict[int, list[str]],
    ) -> dict[int, tuple[list[float], list[float]]]:
        """Compute audio+lyrics centroid per cluster from QDrant vectors."""
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
        self, track_ids: list[str],
    ) -> list[tuple[str, list[float], list[float]]]:
        """Fetch audio + lyrics vectors. Skips missing."""
        results: list[tuple[str, list[float], list[float]]] = []
        tasks = [
            asyncio.gather(
                asyncio.to_thread(self.qdrant_repo.retrieve, _AUDIO_COLLECTION, tid),
                asyncio.to_thread(self.qdrant_repo.retrieve, _LYRICS_COLLECTION, tid),
            )
            for tid in track_ids
        ]
        vectors = await asyncio.gather(*tasks, return_exceptions=True)
        for tid, result in zip(track_ids, vectors):
            if isinstance(result, Exception):
                continue
            audio, lyrics = result
            if audio is not None and lyrics is not None:
                results.append((tid, audio, lyrics))
        return results

    async def _fused_knn_search(
        self,
        audio_vector: list[float] | None,
        lyrics_vector: list[float] | None,
        exclude_ids: set[str],
        limit: int,
        filters: dict | None = None,
    ) -> list[RecommendedTrack]:
        """Fused KNN across audio + lyrics collections."""
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
            pid: score for pid, score, _ in audio_hits if pid not in exclude_ids
        }
        lyrics_scores: dict[str, float] = {
            pid: score for pid, score, _ in lyrics_hits if pid not in exclude_ids
        }

        # Collect payloads from both results for track metadata.
        payloads: dict[str, dict] = {}
        for pid, _, payload in audio_hits:
            if pid not in exclude_ids:
                payloads[pid] = payload
        for pid, _, payload in lyrics_hits:
            if pid not in exclude_ids and pid not in payloads:
                payloads[pid] = payload

        all_ids = set(audio_scores) | set(lyrics_scores)
        if not all_ids:
            return []

        if audio_scores and lyrics_scores:
            fused = [
                (pid, _AUDIO_WEIGHT * audio_scores.get(pid, 0.0) + _LYRICS_WEIGHT * lyrics_scores.get(pid, 0.0))
                for pid in all_ids
            ]
        elif audio_scores:
            fused = [(pid, score * _AUDIO_WEIGHT) for pid, score in audio_scores.items()]
        else:
            fused = [(pid, score * _LYRICS_WEIGHT) for pid, score in lyrics_scores.items()]

        fused.sort(key=lambda x: x[1], reverse=True)
        top = fused[:limit]

        results: list[RecommendedTrack] = []
        for pid, score in top:
            payload = payloads.get(pid, {})
            track = _track_from_payload(pid, payload)
            results.append(RecommendedTrack(track=track, similarity_score=score))

        return results

    async def _knn_raw(
        self, collection: str, vector: list[float], limit: int, filters: dict | None = None,
    ) -> list[tuple[str, float, dict]]:
        if filters is None:
            filters = {"status": "ready"}
        try:
            return await asyncio.to_thread(
                self.qdrant_repo.search, collection, vector, limit=limit, filters=filters,
            )
        except Exception as exc:
            logger.warning("knn_search_failed", collection=collection, error=str(exc))
            return []


async def _empty_coro() -> list:
    return []
