"""Mood tags API router.

Endpoints:
    GET /tags                Get mood tags (excluding covered vibes)
    GET /recommendations     Enhanced with tag_id parameter (handled in recommendations.py)
"""

from fastapi import APIRouter, Depends, Query
from karaoke_shared.models.mood_tag import MoodTagResponse
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

from app.dependencies import get_sqlite_repo

router = APIRouter()


@router.get(
    "/tags",
    response_model=list[MoodTagResponse],
    summary="Get mood tags for the session",
)
async def get_tags(
    session_id: str = Query(..., description="Session UUID"),
    limit: int = Query(8, ge=1, le=30, description="Max tags to return"),
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> list[MoodTagResponse]:
    """Return mood tags from vibes not yet covered by the session.

    Looks at which catalog clusters the session has already sung from
    and excludes tags belonging to those clusters.
    """
    # Find which clusters are covered by the session's play history
    history = await repo.get_history_by_session(session_id)
    played_track_ids = [entry.track_id for entry in history]

    covered_cluster_ids: set[int] = set()
    if played_track_ids:
        tracks_map = await repo.get_tracks_by_ids(played_track_ids)
        for track in tracks_map.values():
            if track.catalog_cluster_id is not None:
                covered_cluster_ids.add(track.catalog_cluster_id)

    # Get tags from uncovered clusters
    tag_rows = await repo.get_tags_excluding_clusters(covered_cluster_ids, limit)

    return [
        MoodTagResponse(id=row["id"], name=row["name"])
        for row in tag_rows
    ]
