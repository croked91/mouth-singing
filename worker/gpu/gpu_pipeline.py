"""GPU audio processing pipeline (v3-rc1 mode).

Orchestrates the 10-step pipeline using local GPU hardware:
  1. UVR separation (GPU, BS-Roformer)
  2. Feature extraction (CPU, parallel with 3+4)
  3. VAD on vocals (CPU, parallel with 2)
  4. Whisper ASR (GPU, after VAD)
  5. LLM lyrics search (API)
  6+7. CTC alignment (GPU, torchaudio MMS_FA)
  8. Line break detection (CPU)
  9. Lyric embedding (GPU/CPU)
  10. QDrant sync
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import structlog

from karaoke_shared.models.job import Job
from karaoke_shared.models.track import TrackUpdate
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
from karaoke_shared.services.job_service import JobService

from worker.common.base_pipeline import BasePipeline
from worker.common.ctc_aligner import CTCAligner
from worker.common.lyrics_agent import LyricsAgent
from worker.common.lyrics_searcher import LyricsSearchError
from worker.common.vad_processor import VADProcessor
from worker.gpu.uvr_separator import UVRSeparator
from worker.gpu.whisper_transcriber import WhisperTranscriber

logger = structlog.get_logger(__name__)


class GpuPipeline(BasePipeline):
    """Orchestrates the v3-rc1 GPU audio processing pipeline.

    Args:
        job_service: For updating job state.
        uvr: Vocal/instrumental separator (local GPU).
        repo: SQLite repository.
        whisper: faster-whisper ASR transcriber.
        vad_processor: Voice activity detector.
        lyrics_searcher: Agent-based lyrics finder (optional).
        ctc_aligner: CTC forced alignment.
        feature_extractor: Audio feature extractor (optional).
        lyric_embedder: Lyrics embedder (optional).
        qdrant_repo: QDrant repository (optional).
        settings: Worker settings instance.
    """

    def __init__(
        self,
        job_service: JobService,
        uvr: UVRSeparator,
        repo: SQLiteRepository,
        whisper: WhisperTranscriber,
        vad_processor: VADProcessor,
        lyrics_searcher: LyricsAgent | None,
        ctc_aligner: CTCAligner,
        feature_extractor: object | None = None,
        lyric_embedder: object | None = None,
        qdrant_repo: QDrantRepository | None = None,
        settings: object | None = None,
    ) -> None:
        self.job_service = job_service
        self.uvr = uvr
        self.repo = repo
        self.whisper = whisper
        self.vad_processor = vad_processor
        self.lyrics_searcher = lyrics_searcher
        self.ctc_aligner = ctc_aligner
        self.feature_extractor = feature_extractor
        self.lyric_embedder = lyric_embedder
        self.qdrant_repo = qdrant_repo
        self.settings = settings

    async def process(self, job: Job) -> None:
        """Run the full GPU pipeline for a single job."""
        track = await self.repo.get_track(job.track_id)
        if track is None:
            await self.job_service.mark_failed(
                job.id, f"Track {job.track_id} not found"
            )
            return

        if not track.mp3_path:
            await self.job_service.mark_failed(
                job.id, f"Track {job.track_id} has no mp3_path"
            )
            return

        try:
            pipeline_t0 = time.monotonic()

            # ==============================================================
            # STEP 1: UVR separation (GPU, ~60-90s)
            # ==============================================================
            await self.job_service.mark_step(job.id, "separating", 0)

            vocals_path, instrumental_path = await self._separate_with_fallback(
                track.mp3_path
            )

            # Free VRAM before Whisper.
            await asyncio.to_thread(self.uvr.cleanup)

            await self.repo.update_track(
                job.track_id,
                TrackUpdate(instrumental_path=instrumental_path, status="processing"),
            )
            await self.job_service.mark_step(job.id, "separating", 100)

            # ==============================================================
            # PARALLEL: Step 2 (features) + Steps 3+4 (VAD + ASR)
            # ==============================================================
            feature_vector, whisper_result = await asyncio.gather(
                self._extract_features(track.mp3_path, job.id),
                self._vad_and_transcribe(vocals_path, job.id),
            )

            # Free Whisper VRAM.
            await asyncio.to_thread(self.whisper.cleanup)

            # ==============================================================
            # STEP 5: LLM lyrics search (~2-5s)
            # ==============================================================
            await self.job_service.mark_step(job.id, "searching_lyrics", 0)

            if self.lyrics_searcher is None:
                await self.job_service.mark_permanently_failed(
                    job.id, "Lyrics agent not configured (check DEEPSEEK/YANDEX env vars)"
                )
                return

            artist_hint, title_hint = self._parse_hints_from_path(track.mp3_path)

            try:
                lyrics_result = await self.lyrics_searcher.search(
                    asr_text=whisper_result.text,
                    detected_language=whisper_result.language,
                    artist_hint=artist_hint or track.artist,
                    title_hint=title_hint or track.title,
                )
            except LyricsSearchError as exc:
                logger.error("lyrics_search_failed", job_id=job.id, error=str(exc))
                await self.repo.update_track(
                    job.track_id,
                    TrackUpdate(
                        status="error",
                        error_message=f"Lyrics search failed: {exc}",
                    ),
                )
                if feature_vector is not None:
                    await self._sync_qdrant_audio_only(
                        job.track_id, track, feature_vector
                    )
                await self.job_service.mark_permanently_failed(
                    job.id, f"Lyrics search failed: {exc}"
                )
                return

            await self.job_service.mark_step(job.id, "searching_lyrics", 100)

            # Update artist/title from LLM.
            await self.repo.update_track(
                job.track_id,
                TrackUpdate(
                    artist=lyrics_result.artist,
                    title=lyrics_result.title,
                    lyrics_text=lyrics_result.lyrics,
                    language=lyrics_result.language,
                ),
            )

            # ==============================================================
            # STEPS 6+7: CTC alignment (GPU via torchaudio)
            # ==============================================================
            await self.job_service.mark_step(job.id, "aligning", 0)

            syllable_timings, align_stats = await asyncio.to_thread(
                self.ctc_aligner.align,
                vocals_path,
                lyrics_result.lyrics,
                lyrics_result.language,
            )

            # Free CTC model VRAM.
            if hasattr(self.ctc_aligner, "cleanup"):
                await asyncio.to_thread(self.ctc_aligner.cleanup)

            await self.job_service.mark_step(job.id, "aligning", 100)
            logger.info(
                "ctc_alignment_done",
                job_id=job.id,
                total_words=align_stats.total_words,
                char_level=align_stats.char_level_used,
                fallback=align_stats.proportional_fallback,
            )

            # ==============================================================
            # STEP 8: Line break detection (CPU, fast)
            # ==============================================================
            from karaoke_shared.utils.line_breaker import detect_line_breaks

            syllable_timings = await asyncio.to_thread(
                detect_line_breaks, syllable_timings, vocals_path
            )

            # Clean up vocal files after line break detection.
            Path(vocals_path).unlink(missing_ok=True)
            cleaned_path = Path(vocals_path).parent / "cleaned_vocals.wav"
            cleaned_path.unlink(missing_ok=True)

            await self.repo.update_track(
                job.track_id,
                TrackUpdate(syllable_timings=syllable_timings, status="processing"),
            )

            # ==============================================================
            # STEP 9: Lyric embedding
            # ==============================================================
            lyric_vector: list[float] | None = None

            if self.lyric_embedder is not None:
                await self.job_service.mark_step(job.id, "embedding_lyrics", 0)
                lyric_vector = await asyncio.to_thread(
                    self.lyric_embedder.embed, lyrics_result.lyrics
                )
                if hasattr(self.lyric_embedder, "cleanup"):
                    await asyncio.to_thread(self.lyric_embedder.cleanup)
                await self.job_service.mark_step(job.id, "embedding_lyrics", 100)

            # ==============================================================
            # STEP 10: QDrant sync
            # ==============================================================
            await self._sync_qdrant(
                job.id, job.track_id, track, feature_vector, lyric_vector
            )

            # Finalize.
            await self.repo.update_track(
                job.track_id,
                TrackUpdate(status="ready", qdrant_synced=1, mp3_path=None),
            )
            await self.job_service.mark_completed(job.id, {
                "instrumental_path": instrumental_path,
                "language": lyrics_result.language,
                "align_stats": {
                    "total_words": align_stats.total_words,
                    "char_level_used": align_stats.char_level_used,
                },
            })

            logger.info(
                "pipeline_completed",
                job_id=job.id,
                track_id=job.track_id,
                total_duration_sec=round(time.monotonic() - pipeline_t0, 2),
            )

            # Remove the original upload — instrumental is sufficient.
            if track.mp3_path:
                original = Path(track.mp3_path)
                if original.exists():
                    original.unlink()
                    logger.info("original_mp3_deleted", path=str(original))

        except Exception as exc:
            logger.error("pipeline_failed", job_id=job.id, error=str(exc), exc_info=True)
            await self.job_service.mark_permanently_failed(job.id, str(exc))

    def cleanup(self) -> None:
        """Release GPU resources (UVR + Whisper + CTC models)."""
        self.uvr.cleanup()
        self.whisper.cleanup()
        if hasattr(self.ctc_aligner, "cleanup"):
            self.ctc_aligner.cleanup()

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    async def _separate_with_fallback(self, mp3_path: str) -> tuple[str, str]:
        """Try GPU separation, fall back to CPU on OOM."""
        try:
            return await asyncio.to_thread(self.uvr.separate, mp3_path)
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower() or "cuda" in str(exc).lower():
                logger.warning("uvr_cuda_oom_fallback", error=str(exc))
                await asyncio.to_thread(self.uvr.cleanup)
                import os
                os.environ["CUDA_VISIBLE_DEVICES"] = ""
                self.uvr = UVRSeparator(
                    model_cache_dir=self.uvr.model_cache_dir,
                    media_root=self.uvr.media_root,
                    model_name=self.uvr._model_name,
                )
                return await asyncio.to_thread(self.uvr.separate, mp3_path)
            raise

    async def _extract_features(
        self, mp3_path: str, job_id: str
    ) -> list[float] | None:
        """Extract audio features (runs in parallel with ASR)."""
        if self.feature_extractor is None:
            return None
        await self.job_service.mark_step(job_id, "extracting_features", 0)
        try:
            result = await asyncio.to_thread(
                self.feature_extractor.extract, mp3_path
            )
            await self.job_service.mark_step(job_id, "extracting_features", 100)
            return result
        except Exception as exc:
            logger.warning("feature_extraction_failed", error=str(exc))
            return None

    async def _vad_and_transcribe(
        self, vocals_path: str, job_id: str
    ):
        """VAD + faster-whisper ASR (sequential within the parallel branch)."""
        await self.job_service.mark_step(job_id, "transcribing", 0)
        cleaned_path = await asyncio.to_thread(
            self.vad_processor.process, vocals_path
        )
        result = await asyncio.to_thread(self.whisper.transcribe, cleaned_path)
        await self.job_service.mark_step(job_id, "transcribing", 100)
        return result

    async def _sync_qdrant(
        self,
        job_id: str,
        track_id: str,
        track,
        feature_vector: list[float] | None,
        lyric_vector: list[float] | None,
    ) -> None:
        """Sync audio features and lyrics embeddings to QDrant."""
        if self.qdrant_repo is None:
            return

        await self.job_service.mark_step(job_id, "syncing_qdrant", 0)
        logger.info("qdrant_sync_starting", track_id=track_id)
        t0 = time.monotonic()

        payload = {
            "track_id": track_id,
            "artist": track.artist,
            "title": track.title,
            "status": "ready",
        }

        if feature_vector is not None and any(v != 0.0 for v in feature_vector):
            await asyncio.to_thread(
                self.qdrant_repo.upsert,
                "audio_features",
                track_id,
                feature_vector,
                payload,
            )

        if lyric_vector is not None and any(v != 0.0 for v in lyric_vector):
            await asyncio.to_thread(
                self.qdrant_repo.upsert,
                "lyrics_embeddings",
                track_id,
                lyric_vector,
                payload,
            )

        logger.info(
            "qdrant_sync_completed",
            track_id=track_id,
            duration_sec=round(time.monotonic() - t0, 2),
        )
        await self.job_service.mark_step(job_id, "syncing_qdrant", 100)

    async def _sync_qdrant_audio_only(
        self,
        track_id: str,
        track,
        feature_vector: list[float],
    ) -> None:
        """Sync only audio features when lyrics search fails."""
        if self.qdrant_repo is None or not any(v != 0.0 for v in feature_vector):
            return

        payload = {
            "track_id": track_id,
            "artist": track.artist,
            "title": track.title,
            "status": "error",
        }

        try:
            await asyncio.to_thread(
                self.qdrant_repo.upsert,
                "audio_features",
                track_id,
                feature_vector,
                payload,
            )
        except Exception as exc:
            logger.warning("qdrant_audio_only_sync_failed", error=str(exc))

    @staticmethod
    def _parse_hints_from_path(mp3_path: str) -> tuple[str | None, str | None]:
        """Extract artist/title from filename like 'Artist - Title.mp3'."""
        name = Path(mp3_path).stem
        if " - " in name:
            parts = name.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return None, None
