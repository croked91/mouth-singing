"""Track service — business logic for track uploads and retrieval.

Handles saving uploaded MP3 files to disk and creating the corresponding
track and job records in the database.
"""

from __future__ import annotations

import asyncio
import pathlib
import uuid

import structlog
from karaoke_shared.models.job import Job, JobCreate
from karaoke_shared.models.track import Track, TrackCreate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

logger = structlog.get_logger(__name__)

# Maximum file size accepted for uploads (50 MB).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class TrackService:
    """Orchestrates track creation and upload processing.

    Args:
        repo: An open SQLiteRepository for the current request.
        media_root: Absolute path to the media storage directory.
    """

    def __init__(self, repo: SQLiteRepository, media_root: str) -> None:
        self.repo = repo
        self.media_root = media_root

    async def upload_mp3(
        self,
        content: bytes,
        artist: str | None,
        title: str | None,
    ) -> Track:
        """Save uploaded MP3 bytes to disk and create a pending track record.

        The file is written to ``{media_root}/uploads/{track_id}.mp3``.
        The track is created with ``status='pending'`` and
        ``source='user_upload'``.

        Args:
            content: The raw MP3 file bytes (already read by the router).
            artist: Artist name, or ``None`` to use "Unknown Artist".
            title: Track title, or ``None`` to use "Unknown Track".

        Returns:
            The newly created Track record.
        """
        track_id = str(uuid.uuid4())
        resolved_artist = artist if artist else "Unknown Artist"
        resolved_title = title if title else "Unknown Track"

        # Build the upload destination and ensure the directory exists.
        uploads_dir = pathlib.Path(self.media_root) / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        dest_path = uploads_dir / f"{track_id}.mp3"

        # Write the file off the event loop thread.
        await asyncio.to_thread(dest_path.write_bytes, content)

        logger.info(
            "track_file_saved",
            track_id=track_id,
        )

        track = await self.repo.create_track(
            TrackCreate(
                id=track_id,
                artist=resolved_artist,
                title=resolved_title,
                mp3_path=str(dest_path),
                source="user_upload",
                status="pending",
            )
        )

        logger.info("track_created", track_id=track_id)
        return track

    async def get_track(self, track_id: str) -> Track | None:
        """Return a track by ID, or ``None`` if not found.

        Args:
            track_id: The UUID string identifying the track.

        Returns:
            The Track record, or ``None``.
        """
        return await self.repo.get_track(track_id)

    async def list_popular(self, limit: int = 10) -> list[Track]:
        """Return the most-played ready tracks.

        Delegates to the repository, which already filters on status='ready'
        and orders by play_count descending.

        Args:
            limit: Maximum number of tracks to return.

        Returns:
            A list of Track records.
        """
        return await self.repo.list_popular(limit)

    async def enqueue_processing(self, track_id: str) -> Job:
        """Create a job in the job queue to process the given track.

        The job starts with status='pending' and default priority=1.

        Args:
            track_id: The ID of the track to process.

        Returns:
            The newly created Job record.
        """
        job = await self.repo.create_job(JobCreate(track_id=track_id))
        logger.info("job_enqueued", track_id=track_id, job_id=job.id)
        return job
