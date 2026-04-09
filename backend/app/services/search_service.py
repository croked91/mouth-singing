"""Search service — hybrid FTS5 + semantic search over the track catalog.

The service tries FTS5 first. If the result set is smaller than a threshold
(fewer than 5 matches) and a sentence-transformers Embedder is available, it
falls back to a semantic search in QDrant and merges the two result sets.
FTS results are always preferred over semantic results during deduplication.
"""

from __future__ import annotations

import asyncio

import structlog
from karaoke_shared.constants import COLLECTION_LYRICS_EMBEDDINGS, TrackStatus
from karaoke_shared.models.track import Track
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.pg_repository import PgRepository
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

_SEMANTIC_FALLBACK_THRESHOLD = 5

_LYRICS_COLLECTION = COLLECTION_LYRICS_EMBEDDINGS


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
    """Hybrid search combining FTS5 and optional semantic vector search.

    Args:
        sqlite_repo: Open repository for FTS and autocomplete queries.
        qdrant_repo: Repository for vector similarity queries.
        embedder: Optional Embedder for semantic search. ``None`` means
            the service operates in FTS-only mode.
    """

    def __init__(
        self,
        sqlite_repo: PgRepository,
        qdrant_repo: QDrantRepository,
        embedder: object | None,
    ) -> None:
        self.sqlite_repo = sqlite_repo
        self.qdrant_repo = qdrant_repo
        self.embedder = embedder

    async def search(
        self, query: str, limit: int = 20, offset: int = 0,
    ) -> SearchResult:
        """Run a hybrid search and return a combined, deduplicated result set.

        Args:
            query: The user's search string.
            limit: Maximum number of results to return.
            offset: Number of results to skip (for pagination).
        """
        # Run FTS and count in parallel for accurate pagination total.
        fts_tracks, fts_total = await asyncio.gather(
            self.sqlite_repo.search_fts(query, limit=limit + offset, offset=0),
            self.sqlite_repo.search_fts_count(query),
        )

        semantic_tracks: list[Track] = []
        should_do_semantic = (
            len(fts_tracks) < _SEMANTIC_FALLBACK_THRESHOLD
            and self.embedder is not None
        )

        if should_do_semantic:
            semantic_tracks = await self._semantic_search(query, limit=limit)

        merged = self._merge_results(fts_tracks, semantic_tracks)

        total = max(fts_total, len(merged))
        paged = merged[offset : offset + limit]

        # Batch-fetch artist images.
        artist_names = list({t.artist for t in paged})
        artists_map = await self.sqlite_repo.get_artists_by_names(artist_names)
        artist_images: dict[str, str | None] = {}
        for name in artist_names:
            artist = artists_map.get(name)
            if artist and artist.get("image_path"):
                artist_images[name] = f"/api/v1/media/artists/{artist['image_path']}"
            else:
                artist_images[name] = None

        items = [self._track_to_search_item(t, artist_images.get(t.artist)) for t in paged]

        return SearchResult(total=total, items=items)

    async def mood_search(
        self,
        query: str,
        mood_expander: object | None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResult:
        """Pure semantic search with optional LLM query expansion.

        Bypasses FTS entirely — expands the query into lyrics-like text
        via DeepSeek, then searches QDrant directly.  Falls back to
        embedding the raw query if the expander is unavailable.
        """
        if self.embedder is None:
            return SearchResult(total=0, items=[])

        if mood_expander is not None:
            expanded = await mood_expander.expand(query)
        else:
            expanded = query

        logger.info("mood_search", query=query, expanded=expanded[:120])

        tracks = await self._semantic_search(expanded, limit=limit + offset)
        paged = tracks[offset: offset + limit]

        artist_names = list({t.artist for t in paged})
        artists_map = await self.sqlite_repo.get_artists_by_names(artist_names)
        artist_images: dict[str, str | None] = {}
        for name in artist_names:
            artist = artists_map.get(name)
            artist_images[name] = (
                f"/api/v1/media/artists/{artist['image_path']}"
                if artist and artist.get("image_path") else None
            )

        items = [self._track_to_search_item(t, artist_images.get(t.artist)) for t in paged]
        return SearchResult(total=len(tracks), items=items)

    async def suggest(self, query: str, limit: int = 10) -> list[str]:
        """Return autocomplete suggestions as "artist — title" strings.

        Runs a LIKE prefix search on artist and title for tracks that have
        status='ready'.

        Args:
            query: The prefix string typed by the user.
            limit: Maximum number of suggestions.

        Returns:
            A list of formatted "artist — title" strings.
        """
        if not query:
            return []

        rows = await self.sqlite_repo.suggest_tracks(query, limit)
        return [f"{row['artist']} — {row['title']}" for row in rows]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _semantic_search(self, query: str, limit: int) -> list[Track]:
        """Embed the query and search the lyrics_embeddings QDrant collection.

        Returns a list of Track records for the matching point IDs. Any
        point whose track cannot be found in SQLite is silently dropped.
        """
        try:
            vector = await asyncio.to_thread(self.embedder.embed, query)
            hits = await asyncio.to_thread(
                self.qdrant_repo.search,
                _LYRICS_COLLECTION,
                vector,
                limit,
            )
        except Exception as exc:
            logger.warning("semantic_search_failed", error=str(exc))
            return []

        # hits is list[tuple[str, float, dict]] — id, score, payload
        point_ids = [point_id for point_id, _score, _payload in hits]
        tracks_map = await self.sqlite_repo.get_tracks_by_ids(point_ids)
        # Preserve QDrant relevance ordering.
        return [tracks_map[pid] for pid in point_ids if pid in tracks_map]

    def _merge_results(
        self, fts_tracks: list[Track], semantic_tracks: list[Track]
    ) -> list[Track]:
        """Merge two track lists, deduplicating by ID.

        FTS results come first; semantic results are appended only when their
        track ID is not already in the FTS list.
        """
        seen_ids: set[str] = set()
        merged: list[Track] = []

        for track in fts_tracks:
            if track.id not in seen_ids:
                seen_ids.add(track.id)
                merged.append(track)

        for track in semantic_tracks:
            if track.id not in seen_ids:
                seen_ids.add(track.id)
                merged.append(track)

        return merged

    def _track_to_search_item(self, track: Track, artist_image_url: str | None = None) -> TrackSearchItem:
        """Convert a full Track record to the condensed TrackSearchItem."""
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
