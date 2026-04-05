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
from karaoke_shared.repositories.pg_repository import PgRepository

from app.dependencies import get_qdrant_repo, get_repo
from app.services.recommendation_service import RecommendationService

router = APIRouter()


async def _to_items(
    recommended, repo: PgRepository
) -> list[RecommendedTrackItem]:
    """Convert RecommendedTrack list to API items with artist images."""
    # Batch-fetch artist images in a single query.
    artist_names = list({r.track.artist for r in recommended})
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
            id=r.track.id,
            artist=r.track.artist,
            title=r.track.title,
            duration_sec=r.track.duration_sec,
            similarity_score=r.similarity_score,
            artist_image_url=artist_images.get(r.track.artist),
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
    language: str | None = Query(None, description="Language filter (e.g. 'ru')"),
    limit: int = Query(5, ge=1, le=50, description="Max results"),
    exclude_ids: str | None = Query(None, description="Comma-separated track IDs to exclude"),
    repo: PgRepository = Depends(get_repo),
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
        cluster = next((c for c in clusters if c.id == tag["cluster_id"]), None)
        if cluster is None:
            return RecommendationResponse(strategy=RecommendationStrategy.POPULAR, tracks=[])

        strategy, results = await service.get_tag_recommendations(
            tag_centroid_audio=cluster.centroid_audio,
            tag_centroid_lyrics=cluster.centroid_lyrics,
            session_id=session_id,
            limit=limit * 3,  # oversample for artist dedup
            language=language,
            tag_cluster_id=cluster.id,
        )
        # Deduplicate by artist.
        seen_artists: set[str] = set()
        deduped = []
        for r in results:
            if r.track.artist not in seen_artists:
                seen_artists.add(r.track.artist)
                deduped.append(r)
                if len(deduped) >= limit:
                    break
        return RecommendationResponse(
            strategy=strategy,
            tracks=await _to_items(deduped, repo),
        )

    # Auto-recommendations
    extra_exclude = set(exclude_ids.split(",")) if exclude_ids else None
    strategy, recommended = await service.get_recommendations(
        session_id=session_id,
        limit=limit,
        language=language,
        extra_exclude_ids=extra_exclude,
    )
    return RecommendationResponse(strategy=strategy, tracks=await _to_items(recommended, repo))
