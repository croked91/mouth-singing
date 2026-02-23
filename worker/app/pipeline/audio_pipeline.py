"""Audio processing pipeline.

Orchestrates the full audio processing pipeline for a single job. In Phase 7a,
only Step 1 (UVR vocal/instrumental separation) is implemented. Steps 2-6 are
stub comments with TODO markers for Phase 7b and Phase 8a.
"""

from __future__ import annotations

import asyncio

import structlog

from karaoke_shared.models.job import Job
from karaoke_shared.models.track import TrackUpdate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
from karaoke_shared.services.job_service import JobService

from app.pipeline.uvr_separator import UVRSeparator

logger = structlog.get_logger(__name__)


class AudioPipeline:
    """Orchestrates the full audio processing pipeline.

    Each public method is a pipeline stage. In Phase 7a only Step 1 (UVR
    separation) is wired up; later steps exist as comments so the overall
    structure is clear.

    Args:
        job_service: Service for updating job state throughout processing.
        uvr: The vocal/instrumental separator.
        repo: Repository for reading track data and persisting updates.
    """

    def __init__(
        self,
        job_service: JobService,
        uvr: UVRSeparator,
        repo: SQLiteRepository,
    ) -> None:
        self.job_service = job_service
        self.uvr = uvr
        self.repo = repo

    async def process(self, job: Job) -> None:
        """Run the full pipeline for a single job.

        Fetches the associated track, runs each implemented pipeline step,
        and marks the job completed or failed. Any unhandled exception causes
        the job to be handed back to the retry machinery via ``mark_failed``.

        Args:
            job: The locked job to process.
        """
        track = await self.repo.get_track(job.track_id)
        if track is None:
            await self.job_service.mark_failed(job.id, f"Track {job.track_id} not found")
            return

        if not track.mp3_path:
            await self.job_service.mark_failed(
                job.id, f"Track {job.track_id} has no mp3_path"
            )
            return

        try:
            # ------------------------------------------------------------------
            # Step 1: UVR separation (Phase 7a)
            # ------------------------------------------------------------------
            await self.job_service.mark_step(job.id, "separating", 0)

            vocals_path, instrumental_path = await asyncio.to_thread(
                self.uvr.separate, track.mp3_path
            )

            await self.job_service.mark_step(job.id, "separating", 100)

            # Persist the instrumental path on the track record.
            await self.repo.update_track(
                job.track_id,
                TrackUpdate(instrumental_path=instrumental_path, status="processing"),
            )

            logger.info(
                "step_completed",
                job_id=job.id,
                step="separating",
                instrumental_path=instrumental_path,
            )

            # ------------------------------------------------------------------
            # Step 2: Sonoix transcription (stub — Phase 7b)
            # TODO: transcription = await SonoixClient.transcribe(vocals_path)
            # ------------------------------------------------------------------

            # ------------------------------------------------------------------
            # Step 3: Video generation (stub — Phase 7b)
            # TODO: clip_path = await VideoGenerator.generate(
            #     instrumental_path, transcription, track.artist, track.title
            # )
            # ------------------------------------------------------------------

            # ------------------------------------------------------------------
            # Step 4: Feature extraction (stub — Phase 8a)
            # TODO: feature_vector = await FeatureExtractor.extract(instrumental_path)
            # ------------------------------------------------------------------

            # ------------------------------------------------------------------
            # Step 5: Lyric embedding (stub — Phase 8a)
            # TODO: lyric_vector = await LyricEmbedder.embed(transcription.lyrics_text)
            # ------------------------------------------------------------------

            # ------------------------------------------------------------------
            # Step 6: QDrant sync + SQLite status update (stub — Phase 8a)
            # TODO: await QDrantRepository.upsert(track_id, feature_vector, lyric_vector)
            # TODO: await repo.update_track(track_id, TrackUpdate(status="ready"))
            # ------------------------------------------------------------------

            result = {
                "vocals_path": vocals_path,
                "instrumental_path": instrumental_path,
                "steps_completed": ["separating"],
            }
            await self.job_service.mark_completed(job.id, result)

        except Exception as exc:
            logger.error("pipeline_failed", job_id=job.id, error=str(exc))
            await self.job_service.mark_failed(job.id, str(exc))
