"""Mood tags API router — proxies to rec-service.

Endpoints:
    GET /tags   Get mood tags (excluding covered vibes)
"""

from fastapi import APIRouter, Depends, Query, Request
from karaoke_shared.models.mood_tag import MoodTagResponse
from karaoke_shared.repositories.pg_repository import PgRepository

from app.dependencies import get_repo

router = APIRouter()


@router.get(
    "/tags",
    response_model=list[MoodTagResponse],
    summary="Get mood tags for the session",
)
async def get_tags(
    request: Request,
    session_id: str = Query(..., description="Session UUID"),
    limit: int = Query(8, ge=1, le=30, description="Max tags to return"),
    repo: PgRepository = Depends(get_repo),
) -> list[MoodTagResponse]:
    """Return mood tags from vibes not yet covered by the session.

    Proxies to rec-service. Returns empty list if rec-service is unavailable.
    """
    rec_client = getattr(request.app.state, "rec_client", None)

    # Get played track IDs from PG.
    history = await repo.get_history_by_session(session_id)
    played_track_ids = [entry.track_id for entry in history]

    # Try rec-service.
    if rec_client is not None:
        result = await rec_client.get_tags(played_track_ids, limit)
        if result is not None:
            return [MoodTagResponse(id=t["id"], name=t["name"]) for t in result]

    # Fallback: empty list.
    return []
