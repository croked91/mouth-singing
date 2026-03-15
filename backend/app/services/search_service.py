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
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
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
        sqlite_repo: SQLiteRepository,
        qdrant_repo: QDrantRepository,
        embedder: object | None,
    ) -> None:
        self.sqlite_repo = sqlite_repo
        self.qdrant_repo = qdrant_repo
        self.embedder = embedder

    async def search(
        self, query: str, limit: int = 20, offset: int = 0
    ) -> SearchResult:
        """Run a hybrid search and return a combined, deduplicated result set.

        Steps:
        1. Run FTS5 via the SQLite repository.
        2. If fewer than 5 FTS results were found AND an embedder is available,
           run semantic search in QDrant and fetch the matching Track records.
        3. Merge: FTS results first, then semantic results that are not already
           present in the FTS list.
        4. Apply offset/limit to the merged list and return.

        Args:
            query: The user's search string.
            limit: Maximum number of results to return.
            offset: Number of results to skip (for pagination).

        Returns:
            A SearchResult with total and the paginated items.
        """
        # Fetch FTS results WITHOUT offset so we can merge correctly with
        # semantic results and then paginate the combined list.
        fts_tracks = await self.sqlite_repo.search_fts(
            query, limit=limit + offset, offset=0
        )

        semantic_tracks: list[Track] = []
        should_do_semantic = (
            len(fts_tracks) < _SEMANTIC_FALLBACK_THRESHOLD
            and self.embedder is not None
        )

        if should_do_semantic:
            semantic_tracks = await self._semantic_search(query, limit=limit)

        merged = self._merge_results(fts_tracks, semantic_tracks)

        total = len(merged)
        paged = merged[offset : offset + limit]
        items = [self._track_to_search_item(t) for t in paged]

        return SearchResult(total=total, items=items)

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
        tracks = []
        for point_id, _score, _payload in hits:
            track = await self.sqlite_repo.get_track(point_id)
            if track is not None:
                tracks.append(track)

        return tracks

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

    def _track_to_search_item(self, track: Track) -> TrackSearchItem:
        """Convert a full Track record to the condensed TrackSearchItem."""
        return TrackSearchItem(
            id=track.id,
            artist=track.artist,
            title=track.title,
            duration_sec=track.duration_sec,
            language=track.language,
            source=track.source,
            clip_ready=(track.status == TrackStatus.READY),
        )
