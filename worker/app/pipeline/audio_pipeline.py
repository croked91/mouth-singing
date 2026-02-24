"""Audio processing pipeline.

Orchestrates the full audio processing pipeline for a single job.  Steps
implemented per phase:
  - Phase 7a: Step 1 — UVR vocal/instrumental separation.
  - Phase 7b: Step 2 — Soniox transcription + syllabification.
              Step 3 — VideoGenerator MP4 generation.
  - Phase 8a: Steps 4-6 — feature extraction, lyric embedding, QDrant sync.
"""

from __future__ import annotations

import asyncio

import structlog

from karaoke_shared.models.job import Job
from karaoke_shared.models.track import TrackUpdate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
from karaoke_shared.services.job_service import JobService
from karaoke_shared.utils.syllabifier import Syllabifier

from app.pipeline.sonoix_client import SonoixClient
from app.pipeline.uvr_separator import UVRSeparator
from app.pipeline.video_generator import VideoGenerator

logger = structlog.get_logger(__name__)


class AudioPipeline:
    """Orchestrates the full audio processing pipeline.

    Each pipeline step is implemented as a discrete block within
    :meth:`process`.  Dependencies are injected so that each component can be
    tested or swapped independently.

    ``sonoix`` and ``video_gen`` are optional: when ``None`` the corresponding
    pipeline steps (transcription and video generation) are skipped.  This
    allows unit tests that only exercise the UVR step to construct the pipeline
    without providing Soniox or FFmpeg dependencies.

    Args:
        job_service: Service for updating job state throughout processing.
        uvr: The vocal/instrumental separator.
        repo: Repository for reading track data and persisting updates.
        sonoix: Soniox Speech-to-Text client for transcription.
        video_gen: VideoGenerator for creating the karaoke MP4 clip.
    """

    def __init__(
        self,
        job_service: JobService,
        uvr: UVRSeparator,
        repo: SQLiteRepository,
        sonoix: SonoixClient | None = None,
        video_gen: VideoGenerator | None = None,
    ) -> None:
        self.job_service = job_service
        self.uvr = uvr
        self.repo = repo
        self.sonoix = sonoix
        self.video_gen = video_gen
        self._syllabifier = Syllabifier()

    async def process(self, job: Job) -> None:
        """Run the full pipeline for a single job.

        Fetches the associated track, runs each implemented pipeline step,
        and marks the job completed or failed.  Any unhandled exception causes
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
            # Step 2: Soniox transcription + syllabification (Phase 7b)
            # ------------------------------------------------------------------
            syllable_timings = []
            transcription = None

            if self.sonoix is not None:
                await self.job_service.mark_step(job.id, "transcribing", 0)

                transcription = await self.sonoix.transcribe(vocals_path)
                syllable_timings = self._syllabifier.syllabify(transcription.tokens)

                await self.job_service.mark_step(job.id, "transcribing", 100)

                await self.repo.update_track(
                    job.track_id,
                    TrackUpdate(
                        lyrics_text=transcription.full_text,
                        syllable_timings=syllable_timings,
                        language=transcription.language,
                        status="processing",
                    ),
                )

                logger.info(
                    "step_completed",
                    job_id=job.id,
                    step="transcribing",
                    token_count=len(transcription.tokens),
                    syllable_count=len(syllable_timings),
                    language=transcription.language,
                )

            # ------------------------------------------------------------------
            # Step 3: Video generation (Phase 7b)
            # ------------------------------------------------------------------
            clip_path: str | None = None

            if self.video_gen is not None and transcription is not None:
                await self.job_service.mark_step(job.id, "generating_video", 0)

                clip_path = await self.video_gen.generate(
                    instrumental_path=instrumental_path,
                    syllable_timings=syllable_timings,
                    artist=track.artist,
                    title=track.title,
                    track_id=job.track_id,
                )

                await self.job_service.mark_step(job.id, "generating_video", 100)

                await self.repo.update_track(
                    job.track_id,
                    TrackUpdate(clip_path=clip_path, status="processing"),
                )

                logger.info(
                    "step_completed",
                    job_id=job.id,
                    step="generating_video",
                    clip_path=clip_path,
                )

            # ------------------------------------------------------------------
            # Step 4: Feature extraction (stub — Phase 8a)
            # TODO: feature_vector = await FeatureExtractor.extract(instrumental_path)
            # ------------------------------------------------------------------

            # ------------------------------------------------------------------
            # Step 5: Lyric embedding (stub — Phase 8a)
            # TODO: lyric_vector = await LyricEmbedder.embed(transcription.full_text)
            # ------------------------------------------------------------------

            # ------------------------------------------------------------------
            # Step 6: QDrant sync + SQLite status update (stub — Phase 8a)
            # TODO: await QDrantRepository.upsert(track_id, feature_vector, lyric_vector)
            # TODO: await repo.update_track(track_id, TrackUpdate(status="ready"))
            # ------------------------------------------------------------------

            steps_completed = ["separating"]
            if transcription is not None:
                steps_completed.append("transcribing")
            if clip_path is not None:
                steps_completed.append("generating_video")

            # Mark the track as ready so it appears in search, popular, etc.
            await self.repo.update_track(
                job.track_id, TrackUpdate(status="ready"),
            )

            result = {
                "vocals_path": vocals_path,
                "instrumental_path": instrumental_path,
                "clip_path": clip_path,
                "language": transcription.language if transcription is not None else None,
                "steps_completed": steps_completed,
            }
            await self.job_service.mark_completed(job.id, result)

        except Exception as exc:
            logger.error("pipeline_failed", job_id=job.id, error=str(exc))
            await self.job_service.mark_failed(job.id, str(exc))
