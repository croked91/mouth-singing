"""Bootstrap pipeline — bulk catalog import without Whisper/LLM/VAD.

Simplified GPU pipeline for tracks with pre-existing lyrics.
Skips: Whisper ASR, LLM lyrics search, VAD, line break detection.
CTC receives raw vocals (same as GpuPipeline).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from karaoke_shared.models.job import Job
from karaoke_shared.models.track import TrackUpdate
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
from karaoke_shared.services.job_service import JobService

from worker.common.base_pipeline import BasePipeline
from worker.common.ctc_aligner import CTCAligner
from worker.gpu.uvr_separator import UVRSeparator

logger = structlog.get_logger(__name__)


class BootstrapPipeline(BasePipeline):
    """Pipeline for bulk import with pre-existing lyrics.

    Steps:
      1. UVR Separation (GPU, ~60s) + cleanup
      2. Feature Extraction (CPU, parallel with 3)
      3. CTC Alignment (CPU subprocess, ~25s)
      4. Lyric Embedding
      5. QDrant Sync
      6. Finalization (delete original MP3, mark ready)
    """

    def __init__(
        self,
        job_service: JobService,
        uvr: UVRSeparator,
        repo: SQLiteRepository,
        ctc_aligner: CTCAligner,
        feature_extractor: object | None = None,
        lyric_embedder: object | None = None,
        qdrant_repo: QDrantRepository | None = None,
    ) -> None:
        self.job_service = job_service
        self.uvr = uvr
        self.repo = repo
        self.ctc_aligner = ctc_aligner
        self.feature_extractor = feature_extractor
        self.lyric_embedder = lyric_embedder
        self.qdrant_repo = qdrant_repo

    def cleanup(self) -> None:
        """Release GPU resources."""
        self.uvr.cleanup()

    async def process(self, job: Job) -> None:  # noqa: C901
        try:
            # 0. Retrieve track record
            track = await self.repo.get_track(job.track_id)
            if track is None:
                await self.job_service.mark_failed(
                    job.id, f"Track {job.track_id} not found"
                )
                return

            if not track.lyrics_text or not track.lyrics_text.strip():
                await self.job_service.mark_failed(
                    job.id, "No lyrics_text in track record"
                )
                return

            lyrics_text = track.lyrics_text
            language = track.language or "ru"

            # === STEP 1: UVR Separation (GPU, ~60s) ===
            await self.job_service.mark_step(job.id, "separating", 0)
            vocals_path, instrumental_path = await asyncio.to_thread(
                self.uvr.separate, track.mp3_path
            )
            await asyncio.to_thread(self.uvr.cleanup)
            await self.repo.update_track(
                job.track_id,
                TrackUpdate(
                    instrumental_path=instrumental_path,
                    status="processing",
                ),
            )
            await self.job_service.mark_step(job.id, "separating", 100)

            # === STEP 2+3: Feature Extraction ∥ CTC Alignment ===
            async def _extract() -> list[float] | None:
                if self.feature_extractor is None:
                    return None
                try:
                    return await asyncio.to_thread(
                        self.feature_extractor.extract, track.mp3_path
                    )
                except Exception as exc:
                    logger.warning(
                        "feature_extract_failed",
                        track_id=job.track_id,
                        error=str(exc),
                    )
                    return None

            async def _align():
                return await asyncio.to_thread(
                    self.ctc_aligner.align,
                    vocals_path,  # raw vocals (NOT cleaned by VAD)
                    lyrics_text,
                    language,
                )

            await self.job_service.mark_step(job.id, "aligning", 0)
            feature_vector, (syllable_timings, align_stats) = (
                await asyncio.gather(_extract(), _align())
            )
            await self.job_service.mark_step(job.id, "aligning", 100)

            # Cleanup vocal files (no longer needed)
            Path(vocals_path).unlink(missing_ok=True)

            await self.repo.update_track(
                job.track_id,
                TrackUpdate(syllable_timings=syllable_timings),
            )

            # === STEP 4: Lyric Embedding ===
            lyric_vector = None
            if self.lyric_embedder is not None:
                try:
                    lyric_vector = await asyncio.to_thread(
                        self.lyric_embedder.embed, lyrics_text
                    )
                except Exception as exc:
                    logger.warning(
                        "lyric_embed_failed",
                        track_id=job.track_id,
                        error=str(exc),
                    )

            # === STEP 5: QDrant Sync ===
            if self.qdrant_repo is not None:
                payload = {
                    "track_id": job.track_id,
                    "artist": track.artist,
                    "title": track.title,
                    "status": "ready",
                }
                if feature_vector and any(v != 0.0 for v in feature_vector):
                    await asyncio.to_thread(
                        self.qdrant_repo.upsert,
                        "audio_features",
                        job.track_id,
                        feature_vector,
                        payload,
                    )
                if lyric_vector and any(v != 0.0 for v in lyric_vector):
                    await asyncio.to_thread(
                        self.qdrant_repo.upsert,
                        "lyrics_embeddings",
                        job.track_id,
                        lyric_vector,
                        payload,
                    )

            # === STEP 6: Finalization ===
            if track.mp3_path:
                Path(track.mp3_path).unlink(missing_ok=True)

            await self.repo.update_track(
                job.track_id,
                TrackUpdate(status="ready", qdrant_synced=1, mp3_path=None),
            )
            await self.job_service.mark_completed(
                job.id,
                {
                    "instrumental_path": instrumental_path,
                    "language": language,
                    "align_stats": {
                        "total_words": align_stats.total_words,
                        "char_level_used": align_stats.char_level_used,
                    },
                },
            )
            logger.info(
                "bootstrap_done",
                job_id=job.id,
                track_id=job.track_id,
                artist=track.artist,
                title=track.title,
            )

        except Exception as exc:
            logger.error(
                "bootstrap_failed",
                job_id=job.id,
                error=str(exc),
                exc_info=True,
            )
            await self.job_service.mark_failed(job.id, str(exc))
