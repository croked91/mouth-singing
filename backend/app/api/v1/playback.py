"""Playback router — S3 presigned URL redirect for audio streaming.

Endpoint:
    GET /tracks/{track_id}/stream

Redirects to a presigned S3 URL. The browser/player handles Range requests
directly against S3.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.storage import S3Storage

from app.dependencies import get_repo, get_storage

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get(
    "/tracks/{track_id}/stream",
    summary="Stream a track's audio file via S3 presigned URL",
)
async def stream_track(
    track_id: str,
    repo: PgRepository = Depends(get_repo),
    storage: S3Storage = Depends(get_storage),
):
    """Redirect to a presigned S3 URL for the track's instrumental audio.

    The browser streams directly from S3, which handles Range requests natively.

    Raises:
        404: If the track does not exist or has no associated file.
    """
    track = await repo.get_track(track_id)
    if track is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track '{track_id}' not found.",
        )

    if not track.instrumental_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track '{track_id}' has no associated file.",
        )

    presigned_url = storage.presigned_url(track.instrumental_key)

    logger.debug(
        "stream_redirect",
        track_id=track_id,
        key=track.instrumental_key,
    )

    return RedirectResponse(
        url=presigned_url,
        status_code=status.HTTP_302_FOUND,
    )
