"""Playback router — HTTP Range-Request streaming for audio/video files.

Endpoint:
    GET /tracks/{track_id}/stream

Supports the ``Range: bytes=N-M`` header so that browser media players can
seek without downloading the whole file. Returns 206 Partial Content when a
Range header is present, and 200 OK (via FileResponse) for full-file requests.
"""

from __future__ import annotations

import pathlib

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

from app.config import settings
from app.dependencies import get_sqlite_repo

logger = structlog.get_logger(__name__)

router = APIRouter()

# Map file extension to MIME type for the Content-Type header.
_MIME_BY_EXTENSION: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}

_DEFAULT_CHUNK_SIZE = 64 * 1024  # 64 KB per read


# ---------------------------------------------------------------------------
# Range parsing helper
# ---------------------------------------------------------------------------


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    """Parse an HTTP Range header and return (start, end) byte positions.

    Only the ``bytes=N-M`` format is supported (no multi-range).

    Args:
        range_header: Value of the ``Range`` request header, e.g. "bytes=0-1023".
        file_size: Total size of the file in bytes.

    Returns:
        A ``(start, end)`` tuple where both positions are inclusive and within
        the valid range ``[0, file_size - 1]``.

    Raises:
        HTTPException 416: If the range is not satisfiable.
    """
    if not range_header.startswith("bytes="):
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="Only byte ranges are supported.",
        )

    byte_range = range_header[len("bytes=") :]
    parts = byte_range.split("-", 1)

    try:
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=f"Invalid range: {range_header}",
        )

    # Clamp end to the last valid byte.
    end = min(end, file_size - 1)

    if start > end or start < 0:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=f"Range {start}-{end} is not satisfiable for file of {file_size} bytes.",
        )

    return start, end


# ---------------------------------------------------------------------------
# Streaming generator
# ---------------------------------------------------------------------------


def _file_range_generator(file_path: pathlib.Path, start: int, end: int):
    """Yield chunks of a file between byte positions *start* and *end* inclusive.

    Args:
        file_path: Absolute path to the file on disk.
        start: First byte to include (0-indexed).
        end: Last byte to include (inclusive).

    Yields:
        Bytes chunks of at most ``_DEFAULT_CHUNK_SIZE`` bytes.
    """
    remaining = end - start + 1
    with file_path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk_size = min(_DEFAULT_CHUNK_SIZE, remaining)
            data = fh.read(chunk_size)
            if not data:
                break
            yield data
            remaining -= len(data)


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.get(
    "/tracks/{track_id}/stream",
    summary="Stream a track's audio/video file",
    response_class=StreamingResponse,
)
async def stream_track(
    track_id: str,
    request: Request,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
):
    """Stream a track file with HTTP Range Request support.

    Uses ``clip_path`` (MP4) if the track is ready; falls back to ``mp3_path``
    otherwise. Responds with 206 Partial Content when the client sends a
    ``Range`` header; returns a full-file ``FileResponse`` (200) when there is
    no Range header, letting FastAPI handle the streaming efficiently.

    Raises:
        404: If the track does not exist or has no associated file.
        416: If the requested byte range is not satisfiable.
    """
    track = await repo.get_track(track_id)
    if track is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track '{track_id}' not found.",
        )

    # Prefer instrumental (karaoke) audio; fall back to clip or raw MP3.
    raw_path: str | None = None
    if track.instrumental_path:
        raw_path = track.instrumental_path
    elif track.clip_path:
        raw_path = track.clip_path
    elif track.mp3_path:
        raw_path = track.mp3_path

    if not raw_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track '{track_id}' has no associated file.",
        )

    file_path = pathlib.Path(raw_path).resolve()
    media_root = pathlib.Path(settings.media_root).resolve()
    if not file_path.is_relative_to(media_root):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track '{track_id}' has no associated file.",
        )
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found on disk for track '{track_id}'.",
        )

    extension = file_path.suffix.lower()
    media_type = _MIME_BY_EXTENSION.get(extension, "application/octet-stream")
    file_size = file_path.stat().st_size

    range_header = request.headers.get("range")

    if not range_header:
        # Full file — let FastAPI's FileResponse handle it efficiently.
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            headers={"Accept-Ranges": "bytes"},
        )

    # Partial content response.
    start, end = _parse_range_header(range_header, file_size)
    content_length = end - start + 1

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(content_length),
    }

    logger.debug(
        "stream_range_request",
        track_id=track_id,
        start=start,
        end=end,
        file_size=file_size,
    )

    return StreamingResponse(
        content=_file_range_generator(file_path, start, end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers=headers,
    )
