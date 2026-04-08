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
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.storage import S3Storage
from pydantic import BaseModel

from app.dependencies import get_embedder, get_qdrant_repo, get_repo, get_storage
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
    repo: PgRepository = Depends(get_repo),
    storage: S3Storage = Depends(get_storage),
) -> list[Track]:
    """Return the *limit* most-played tracks that have status='ready'."""
    service = TrackService(repo, storage)
    return await service.list_popular(limit)


@router.get(
    "/search/suggest",
    response_model=list[str],
    summary="Autocomplete suggestions for artist or title",
)
async def suggest(
    q: str = "",
    limit: int = 10,
    repo: PgRepository = Depends(get_repo),
) -> list[str]:
    """Return up to *limit* autocomplete suggestions matching the prefix *q*."""
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
    repo: PgRepository = Depends(get_repo),
    qdrant_repo: QDrantRepository = Depends(get_qdrant_repo),
) -> SearchResult:
    """Hybrid search combining tsvector FTS and optional semantic (vector) search."""
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
    repo: PgRepository = Depends(get_repo),
    storage: S3Storage = Depends(get_storage),
) -> Track:
    """Return the full track record for the given *track_id*."""
    service = TrackService(repo, storage)
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
    request: Request,
    file: UploadFile = FastAPIFile(..., description="MP3 file, max 50 MB"),
    artist: str | None = Form(default=None),
    title: str | None = Form(default=None),
    repo: PgRepository = Depends(get_repo),
    storage: S3Storage = Depends(get_storage),
) -> UploadResponse:
    """Accept a user-uploaded MP3 and enqueue it for processing.

    Validates file extension, content type, and size.
    Uploads to S3 and creates a job record (no track record yet).
    Returns a 202 Accepted response with the job ID.
    """
    content_length_header = request.headers.get("content-length")
    if content_length_header is not None:
        try:
            declared_size = int(content_length_header)
        except ValueError:
            declared_size = None

        if declared_size is not None and declared_size > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File too large: {declared_size} bytes declared in "
                    f"Content-Length. Maximum is {MAX_UPLOAD_BYTES} bytes (50 MB)."
                ),
            )

    filename = file.filename or ""
    extension = pathlib.Path(filename).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Only .mp3 files are accepted. Got '{extension}'.",
        )

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Content-Type must be audio/mpeg. Got '{content_type}'.",
        )

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large: {len(content)} bytes. "
                f"Maximum is {MAX_UPLOAD_BYTES} bytes (50 MB)."
            ),
        )

    rmq = getattr(request.app.state, "rmq", None)
    service = TrackService(repo, storage, rmq)
    job = await service.upload_mp3(content, artist, title, filename=filename)

    logger.info(
        "track_upload_accepted",
        job_id=job.id,
        filename=filename,
    )

    return UploadResponse(
        job_id=job.id,
        status="pending",
    )
