"""Track service — business logic for track uploads and retrieval.

Handles uploading MP3 files to S3 and creating job records.
Tracks are NOT created at upload time — the worker creates them
at pipeline finalisation (deferred track creation).
"""

from __future__ import annotations

import structlog
from karaoke_shared.messaging.rabbitmq import RabbitMQClient
from karaoke_shared.models.job import Job, JobCreate
from karaoke_shared.models.track import Track
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.storage import S3Storage

logger = structlog.get_logger(__name__)

# Maximum file size accepted for uploads (50 MB).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class TrackService:
    """Orchestrates track upload and retrieval.

    Args:
        repo: PgRepository for database access.
        storage: S3Storage for file storage.
    """

    def __init__(
        self,
        repo: PgRepository,
        storage: S3Storage,
        rmq: RabbitMQClient | None = None,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self._rmq = rmq

    async def upload_mp3(
        self,
        content: bytes,
        artist: str | None,
        title: str | None,
        *,
        filename: str | None = None,
        request_id: str | None = None,
    ) -> Job:
        """Upload MP3 to S3 and create a pending job record.

        The file is uploaded to ``uploads/{job_id}.mp3`` in S3.
        A job_queue record is created with mp3_key, artist_hint, title_hint.
        NO track record is created — the worker creates it at finalisation.

        Args:
            content: The raw MP3 file bytes.
            artist: Artist name hint from upload form, or None.
            title: Track title hint from upload form, or None.
            filename: Original filename from upload (for worker parsing).

        Returns:
            The newly created Job record.
        """
        initial_data = {"filename": filename} if filename else None
        job_data = JobCreate(
            artist_hint=artist or None,
            title_hint=title or None,
            data=initial_data,
        )
        job_id = job_data.id

        # Upload to S3
        mp3_key = f"uploads/{job_id}.mp3"
        await self.storage.upload(mp3_key, content)
        job_data.mp3_key = mp3_key

        logger.info("mp3_uploaded_to_s3", job_id=job_id, key=mp3_key)

        # Create job record (no track record)
        job = await self.repo.create_job(job_data)

        # Publish to RabbitMQ for worker consumption. request_id flows through
        # so the worker can stitch its logs to this upload (see RequestIdMiddleware).
        if self._rmq:
            body: dict[str, str] = {"job_id": job_id, "mp3_key": mp3_key}
            if request_id:
                body["request_id"] = request_id
            await self._rmq.publish(
                "jobs", "", body, priority=job_data.priority,
            )
            logger.info("job_published_to_rmq", job_id=job_id)

        logger.info("job_created", job_id=job_id)
        return job

    async def get_track(self, track_id: str) -> Track | None:
        """Return a track by ID, or ``None`` if not found."""
        return await self.repo.get_track(track_id)

    async def list_popular(self, limit: int = 10) -> list[Track]:
        """Return the most-played ready tracks."""
        return await self.repo.list_popular(limit)

    def get_stream_url(self, track: Track) -> str:
        """Generate a presigned URL for streaming a track's instrumental."""
        if not track.instrumental_key:
            raise ValueError(f"Track {track.id} has no instrumental_key")
        return self.storage.presigned_url(track.instrumental_key)
