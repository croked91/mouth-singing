"""Tracks API router.

Endpoints:
    GET    /tracks/popular?limit=10           List most-played ready tracks
    GET    /tracks/search?q=...&limit=20&offset=0  Hybrid FTS + semantic search
    GET    /tracks/search/suggest?q=...&limit=10   Autocomplete suggestions
    GET    /tracks/{track_id}                 Get a single track
    POST   /tracks/upload                     Upload an MP3 (multipart/form-data)
"""

from __future__ import annotations

import pathlib

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from fastapi import File as FastAPIFile
from karaoke_shared.models.track import Track
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
from pydantic import BaseModel

from app.config import settings
from app.dependencies import get_embedder, get_qdrant_repo, get_sqlite_repo
from app.services.search_service import SearchResult, SearchService
from app.services.track_service import MAX_UPLOAD_BYTES, TrackService

logger = structlog.get_logger(__name__)

router = APIRouter()

# Allowed MIME types and file extensions for MP3 uploads.
_ALLOWED_CONTENT_TYPES = {"audio/mpeg", "audio/mp3"}
_ALLOWED_EXTENSIONS = {".mp3"}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    """Response returned after a successful track upload."""

    track_id: str
    job_id: str
    status: str  # Always "pending" at upload time.


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/popular",
    response_model=list[Track],
    summary="List most-played ready tracks",
)
async def list_popular(
    limit: int = 10,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> list[Track]:
    """Return the *limit* most-played tracks that have status='ready'.

    Ordered by play_count descending.
    """
    service = TrackService(repo, settings.media_root)
    return await service.list_popular(limit)


@router.get(
    "/search/suggest",
    response_model=list[str],
    summary="Autocomplete suggestions for artist or title",
)
async def suggest(
    q: str = "",
    limit: int = 10,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> list[str]:
    """Return up to *limit* autocomplete suggestions matching the prefix *q*.

    Each suggestion is formatted as "artist — title".
    Only ready tracks are considered.
    """
    if not q:
        return []

    rows = await repo.suggest_tracks(q, limit)
    return [f"{row['artist']} — {row['title']}" for row in rows]


@router.get(
    "/search",
    response_model=SearchResult,
    summary="Search the track catalog",
)
async def search_tracks(
    request: Request,
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
    qdrant_repo: QDrantRepository = Depends(get_qdrant_repo),
) -> SearchResult:
    """Hybrid search combining FTS5 and optional semantic (vector) search.

    If FTS5 returns fewer than 5 results and a sentence-transformers model
    is available, a semantic search is also run and the results are merged.
    FTS results always take priority in the merged list.
    """
    if not q:
        return SearchResult(total=0, items=[])

    embedder = get_embedder(request)
    service = SearchService(repo, qdrant_repo, embedder)
    return await service.search(q, limit=limit, offset=offset)


@router.get(
    "/{track_id}",
    response_model=Track,
    summary="Get a single track by ID",
)
async def get_track(
    track_id: str,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> Track:
    """Return the full track record for the given *track_id*.

    Raises 404 if no track with that ID exists.
    """
    service = TrackService(repo, settings.media_root)
    track = await service.get_track(track_id)

    if track is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track '{track_id}' not found.",
        )

    return track


@router.post(
    "/upload",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UploadResponse,
    summary="Upload an MP3 file for processing",
)
async def upload_track(
    file: UploadFile = FastAPIFile(..., description="MP3 file, max 50 MB"),
    artist: str | None = Form(default=None),
    title: str | None = Form(default=None),
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> UploadResponse:
    """Accept a user-uploaded MP3 and enqueue it for processing.

    Validates:
    - File extension must be ``.mp3``.
    - Content-Type must be ``audio/mpeg`` or ``audio/mp3``.
    - File size must not exceed 50 MB.

    Returns a 202 Accepted response with the track ID and job ID.
    """
    # Validate file extension.
    filename = file.filename or ""
    extension = pathlib.Path(filename).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Only .mp3 files are accepted. Got '{extension}'.",
        )

    # Validate content type.
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Content-Type must be audio/mpeg. Got '{content_type}'."
            ),
        )

    # Validate file size by reading the whole thing first.
    # We buffer it to check the size without writing a partial file.
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large: {len(content)} bytes. "
                f"Maximum is {MAX_UPLOAD_BYTES} bytes (50 MB)."
            ),
        )

    # Pass the already-read bytes to the service (no second read needed).
    service = TrackService(repo, settings.media_root)
    track = await service.upload_mp3(content, artist, title)
    job = await service.enqueue_processing(track.id)

    logger.info(
        "track_upload_accepted",
        track_id=track.id,
        job_id=job.id,
        filename=filename,
    )

    return UploadResponse(
        track_id=track.id,
        job_id=job.id,
        status="pending",
    )
