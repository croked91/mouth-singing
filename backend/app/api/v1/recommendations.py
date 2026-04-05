"""Recommendations API router — thin proxy to rec-service.

Endpoints:
    GET /recommendations  Get track recommendations for a session
"""

from fastapi import APIRouter, Depends, Query, Request
from karaoke_shared.models.recommendation import (
    RecommendationResponse,
    RecommendedTrackItem,
    RecommendationStrategy,
)
from karaoke_shared.repositories.pg_repository import PgRepository

from app.dependencies import get_repo
from app.services.popular_service import get_popular_tracks

router = APIRouter()


async def _enrich_with_images(
    tracks: list[dict], repo: PgRepository,
) -> list[RecommendedTrackItem]:
    """Add artist_image_url from PG artists table."""
    artist_names = list({t["artist"] for t in tracks})
    artists_map = await repo.get_artists_by_names(artist_names)
    artist_images: dict[str, str | None] = {}
    for name in artist_names:
        artist = artists_map.get(name)
        if artist and artist.get("image_path"):
            artist_images[name] = f"/api/v1/media/artists/{artist['image_path']}"
        else:
            artist_images[name] = None

    return [
        RecommendedTrackItem(
            id=t["id"],
            artist=t["artist"],
            title=t["title"],
            duration_sec=t.get("duration_sec"),
            similarity_score=t.get("similarity_score", 0.0),
            artist_image_url=artist_images.get(t["artist"]),
        )
        for t in tracks
    ]


@router.get(
    "/recommendations",
    response_model=RecommendationResponse,
    summary="Get track recommendations for a session",
)
async def get_recommendations(
    request: Request,
    session_id: str = Query(..., description="Session UUID"),
    tag_id: int | None = Query(None, description="Mood tag ID (overrides auto mode)"),
    language: str | None = Query(None, description="Language filter (e.g. 'ru')"),
    limit: int = Query(5, ge=1, le=50, description="Max results"),
    exclude_ids: str | None = Query(None, description="Comma-separated track IDs to exclude"),
    repo: PgRepository = Depends(get_repo),
) -> RecommendationResponse:
    """Return track recommendations for the session.

    Proxies to rec-service for cluster/tag KNN recommendations.
    Falls back to popular strategy if rec-service is unavailable.
    """
    rec_client = getattr(request.app.state, "rec_client", None)

    # 1. Get play history from PG.
    history = await repo.get_history_by_session(session_id)
    played_ids = [entry.track_id for entry in history]
    exclude_set = set(exclude_ids.split(",")) if exclude_ids else set()

    # 2. If no history and no tag → popular from PG (no rec-service needed).
    if not played_ids and tag_id is None:
        tracks = await get_popular_tracks(repo, limit, language, set(played_ids) | exclude_set)
        return RecommendationResponse(
            strategy=RecommendationStrategy.POPULAR,
            tracks=await _enrich_with_images(tracks, repo),
        )

    # 3. Try rec-service.
    result = None
    if rec_client is not None:
        if tag_id is not None:
            result = await rec_client.get_tag_recommendations(
                tag_id=tag_id,
                played_track_ids=played_ids,
                limit=limit,
                language=language,
            )
        else:
            result = await rec_client.get_recommendations(
                played_track_ids=played_ids,
                limit=limit,
                language=language,
                exclude_ids=list(exclude_set) if exclude_set else None,
            )

    # 4. If rec-service returned results → enrich with artist images.
    if result is not None and result.get("tracks"):
        return RecommendationResponse(
            strategy=RecommendationStrategy(result.get("strategy", "cluster")),
            tracks=await _enrich_with_images(result["tracks"], repo),
        )

    # 5. Fallback: popular from PG.
    tracks = await get_popular_tracks(repo, limit, language, set(played_ids) | exclude_set)
    return RecommendationResponse(
        strategy=RecommendationStrategy.POPULAR,
        tracks=await _enrich_with_images(tracks, repo),
    )
