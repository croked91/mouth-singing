"""Search service — FTS search + mood search (proxied to rec-service).

Title search uses PostgreSQL tsvector FTS.
Mood search calls rec-service for semantic vector search, then re-ranks
results by hit priority and session cluster affinity.
"""

from __future__ import annotations

import asyncio

import structlog
from karaoke_shared.constants import TrackStatus
from karaoke_shared.models.track import Track
from karaoke_shared.repositories.pg_repository import PgRepository
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

_MOOD_OVERSAMPLE = 50
_HIT_CATEGORIES = frozenset({"eternal_hit", "current_hit", "artist_best"})


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TrackSearchItem(BaseModel):
    """Condensed track representation for search result lists."""

    id: str
    artist: str
    title: str
    duration_sec: int | None
    language: str | None
    source: str
    clip_ready: bool  # True when the track has status == "ready"
    artist_image_url: str | None = None


class SearchResult(BaseModel):
    """Search response containing total count and a page of items."""

    total: int
    items: list[TrackSearchItem]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SearchService:
    """FTS search + mood search (via rec-service).

    Args:
        pg_repo: PostgreSQL repository for FTS and track lookups.
        rec_client: HTTP client to rec-service (for mood/semantic search).
    """

    def __init__(
        self,
        pg_repo: PgRepository,
        rec_client: object | None,
    ) -> None:
        self.pg_repo = pg_repo
        self.rec_client = rec_client

    async def search(
        self, query: str, limit: int = 20, offset: int = 0,
    ) -> SearchResult:
        """Full-text search via PostgreSQL tsvector."""
        fts_tracks, fts_total = await asyncio.gather(
            self.pg_repo.search_fts(query, limit=limit + offset, offset=0),
            self.pg_repo.search_fts_count(query),
        )

        paged = fts_tracks[offset: offset + limit]

        artist_names = list({t.artist for t in paged})
        artists_map = await self.pg_repo.get_artists_by_names(artist_names)
        artist_images = self._build_artist_images(artist_names, artists_map)

        items = [self._track_to_search_item(t, artist_images.get(t.artist)) for t in paged]
        return SearchResult(total=fts_total, items=items)

    async def mood_search(
        self,
        query: str,
        mood_expander: object | None,
        limit: int = 10,
        offset: int = 0,
        session_id: str | None = None,
    ) -> SearchResult:
        """Mood/theme search: LLM expansion → rec-service → re-rank.

        Oversamples _MOOD_OVERSAMPLE results from rec-service, then re-ranks:
        tier 1 — hits (eternal_hit, current_hit, artist_best),
        tier 2 — tracks whose cluster matches session play history,
        tier 3 — everything else.
        Within each tier, sorted by mood similarity score.
        """
        if self.rec_client is None:
            return SearchResult(total=0, items=[])

        expanded = await mood_expander.expand(query) if mood_expander else query
        logger.info("mood_search", query=query, expanded=expanded[:120])

        result = await self.rec_client.mood_search(expanded, limit=_MOOD_OVERSAMPLE)
        if not result or not result.get("items"):
            return SearchResult(total=0, items=[])

        scored = [(item, item["similarity_score"]) for item in result["items"]]

        # Session history for warm-session boost.
        played_cluster_ids: set[int] = set()
        if session_id:
            history = await self.pg_repo.get_history_by_session(session_id)
            if history:
                played_ids = [entry.track_id for entry in history]
                played_tracks = await self.pg_repo.get_tracks_by_ids(played_ids)
                played_cluster_ids = {
                    t.rec_cluster_id for t in played_tracks.values()
                    if t.rec_cluster_id is not None
                }

        ranked = self._rerank_mood(scored, played_cluster_ids, limit + offset)
        paged = ranked[offset: offset + limit]

        # Fetch full Track objects from PG for the final page.
        track_ids = [item["id"] for item, _ in paged]
        tracks_map = await self.pg_repo.get_tracks_by_ids(track_ids)

        artist_names = list({t.artist for t in tracks_map.values()})
        artists_map = await self.pg_repo.get_artists_by_names(artist_names)
        artist_images = self._build_artist_images(artist_names, artists_map)

        # Preserve ranked order.
        items: list[TrackSearchItem] = []
        for item, _ in paged:
            track = tracks_map.get(item["id"])
            if track:
                items.append(self._track_to_search_item(track, artist_images.get(track.artist)))

        return SearchResult(total=len(ranked), items=items)

    async def suggest(self, query: str, limit: int = 10) -> list[str]:
        """Return autocomplete suggestions as "artist — title" strings."""
        if not query:
            return []
        rows = await self.pg_repo.suggest_tracks(query, limit)
        return [f"{row['artist']} — {row['title']}" for row in rows]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rerank_mood(
        results: list[tuple[dict, float]],
        played_cluster_ids: set[int],
        limit: int,
    ) -> list[tuple[dict, float]]:
        """Re-rank mood results: hits → cluster matches → rest, each by score."""
        hits: list[tuple[dict, float]] = []
        cluster_match: list[tuple[dict, float]] = []
        rest: list[tuple[dict, float]] = []

        for item, score in results:
            pop = item.get("popularity_category", "regular")
            cid = item.get("rec_cluster_id")
            if pop in _HIT_CATEGORIES:
                hits.append((item, score))
            elif played_cluster_ids and cid in played_cluster_ids:
                cluster_match.append((item, score))
            else:
                rest.append((item, score))

        hits.sort(key=lambda x: x[1], reverse=True)
        cluster_match.sort(key=lambda x: x[1], reverse=True)
        rest.sort(key=lambda x: x[1], reverse=True)

        return (hits + cluster_match + rest)[:limit]

    @staticmethod
    def _build_artist_images(
        artist_names: list[str], artists_map: dict,
    ) -> dict[str, str | None]:
        images: dict[str, str | None] = {}
        for name in artist_names:
            artist = artists_map.get(name)
            images[name] = (
                f"/api/v1/media/artists/{artist['image_path']}"
                if artist and artist.get("image_path") else None
            )
        return images

    @staticmethod
    def _track_to_search_item(track: Track, artist_image_url: str | None = None) -> TrackSearchItem:
        return TrackSearchItem(
            id=track.id,
            artist=track.artist,
            title=track.title,
            duration_sec=track.duration_sec,
            language=track.language,
            source=track.source,
            clip_ready=(track.status == TrackStatus.READY),
            artist_image_url=artist_image_url,
        )
