"""Recommendations API router.

Endpoints:
    GET /recommendations  Get track recommendations for a session (auto or by tag)
"""

from fastapi import APIRouter, Depends, Query
from karaoke_shared.models.recommendation import (
    RecommendationResponse,
    RecommendedTrackItem,
    RecommendationStrategy,
)
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

from app.dependencies import get_qdrant_repo, get_sqlite_repo
from app.services.recommendation_service import RecommendationService

router = APIRouter()


def _to_items(recommended) -> list[RecommendedTrackItem]:
    return [
        RecommendedTrackItem(
            id=r.track.id,
            artist=r.track.artist,
            title=r.track.title,
            duration_sec=r.track.duration_sec,
            similarity_score=r.similarity_score,
        )
        for r in recommended
    ]


@router.get(
    "/recommendations",
    response_model=RecommendationResponse,
    summary="Get track recommendations for a session",
)
async def get_recommendations(
    session_id: str = Query(..., description="Session UUID"),
    tag_id: int | None = Query(None, description="Mood tag ID (overrides auto mode)"),
    limit: int = Query(5, ge=1, le=50, description="Max results"),
    repo: SQLiteRepository = Depends(get_sqlite_repo),
    qdrant_repo: QDrantRepository = Depends(get_qdrant_repo),
) -> RecommendationResponse:
    """Return track recommendations for the session.

    When ``tag_id`` is provided, returns tracks from that tag's cluster
    using KNN search.  Otherwise returns auto-recommendations (POPULAR).
    """
    service = RecommendationService(repo, qdrant_repo)

    if tag_id is not None:
        # Tag-based recommendations: KNN by cluster centroid
        tag = await repo.get_tag(tag_id)
        if tag is None:
            return RecommendationResponse(strategy=RecommendationStrategy.POPULAR, tracks=[])

        clusters = await repo.get_all_clusters()
        cluster = next((c for c in clusters if c["id"] == tag["cluster_id"]), None)
        if cluster is None:
            return RecommendationResponse(strategy=RecommendationStrategy.POPULAR, tracks=[])

        history = await repo.get_history_by_session(session_id)
        played_ids = {entry.track_id for entry in history}

        results = await service._fused_knn_search(
            cluster["centroid_audio"],
            cluster["centroid_lyrics"],
            played_ids,
            limit,
        )
        return RecommendationResponse(
            strategy=RecommendationStrategy.POPULAR,
            tracks=_to_items(results),
        )

    # Auto-recommendations
    strategy, recommended = await service.get_recommendations(
        session_id=session_id,
        limit=limit,
    )
    return RecommendationResponse(strategy=strategy, tracks=_to_items(recommended))
