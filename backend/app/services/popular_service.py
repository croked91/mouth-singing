"""Popular strategy — fallback recommendations from PostgreSQL.

Used when rec-service is unavailable or when the session has no play history.
Pure SQL: no QDrant, no ML, no external dependencies.
"""

from __future__ import annotations

from karaoke_shared.constants import WELL_KNOWN_CATEGORIES
from karaoke_shared.repositories.pg_repository import PgRepository


async def get_popular_tracks(
    repo: PgRepository,
    limit: int = 5,
    language: str | None = None,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    """Return a mix of popular + random tracks as dicts.

    Returns list of {"id", "artist", "title", "duration_sec", "similarity_score"}.
    """
    exclude_ids = exclude_ids or set()
    n_top = max(1, int(limit * 0.7))
    n_random = limit - n_top

    extra = len(exclude_ids) + limit
    top_tracks = await repo.list_popular(limit=n_top + extra, categories=WELL_KNOWN_CATEGORIES)
    random_tracks = await repo.list_random(limit=n_random + extra, categories=WELL_KNOWN_CATEGORIES)

    seen: set[str] = set(exclude_ids)
    results: list[dict] = []

    for t in top_tracks:
        if t.id not in seen and (not language or t.language == language):
            seen.add(t.id)
            results.append({
                "id": t.id,
                "artist": t.artist,
                "title": t.title,
                "duration_sec": t.duration_sec,
                "similarity_score": 0.0,
            })
            if len(results) >= n_top:
                break

    for t in random_tracks:
        if t.id not in seen and (not language or t.language == language):
            seen.add(t.id)
            results.append({
                "id": t.id,
                "artist": t.artist,
                "title": t.title,
                "duration_sec": t.duration_sec,
                "similarity_score": 0.0,
            })
            if len(results) >= limit:
                break

    return results
