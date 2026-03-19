"""Recommendations API router.

Endpoints:
    GET /recommendations  Get track recommendations for a session
"""

from fastapi import APIRouter, Depends, Query
from karaoke_shared.models.recommendation import (
    RecommendationResponse,
    RecommendedTrackItem,
)
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

from app.dependencies import get_qdrant_repo, get_sqlite_repo
from app.services.recommendation_service import RecommendationService

router = APIRouter()


@router.get(
    "/recommendations",
    response_model=RecommendationResponse,
    summary="Get track recommendations for a session",
)
async def get_recommendations(
    session_id: str = Query(..., description="Session UUID"),
    limit: int = Query(5, ge=1, le=50, description="Max results"),
    repo: SQLiteRepository = Depends(get_sqlite_repo),
    qdrant_repo: QDrantRepository = Depends(get_qdrant_repo),
) -> RecommendationResponse:
    """Return track recommendations for the session.

    Currently returns popular tracks.  Future phases will add cluster-based
    recommendations with mood tags and popularity re-ranking.
    """
    service = RecommendationService(repo, qdrant_repo)
    strategy, recommended = await service.get_recommendations(
        session_id=session_id,
        limit=limit,
    )

    items = [
        RecommendedTrackItem(
            id=r.track.id,
            artist=r.track.artist,
            title=r.track.title,
            duration_sec=r.track.duration_sec,
            similarity_score=r.similarity_score,
        )
        for r in recommended
    ]

    return RecommendationResponse(strategy=strategy, tracks=items)
