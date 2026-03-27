"""API audio processing pipeline (v3-rc2 mode).

Orchestrates the pipeline using cloud APIs instead of local GPU:
  1. MVSEP API separation (replaces local UVR)
  2. VAD on vocals (CPU)
  3. Whisper API ASR (replaces local faster-whisper)
  4. Feature extraction (CPU, parallel with 2+3)
  5. LLM lyrics search (API)
  6+7. CTC alignment (CPU)
  8. Line break detection (CPU)
  9. Lyric embedding (CPU or API)
  10. QDrant sync + cost tracking
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

from worker.api.mvsep_client import MVSEPClient
from worker.api.whisper_client import WhisperAPIClient
from worker.common.base_pipeline import BasePipeline
from worker.common.ctc_aligner import CTCAligner
from worker.common.lyrics_agent import LyricsAgent
from worker.common.lyrics_searcher import LyricsSearchError
from worker.common.vad_processor import VADProcessor

logger = structlog.get_logger(__name__)

# Cost constants.
_MVSEP_COST_PER_TRACK = 0.15
_WHISPER_COST_PER_MINUTE = 0.006
_CHAT_COST_ESTIMATE = 0.0003  # DeepSeek agent calls


class ApiPipeline(BasePipeline):
    """Orchestrates the v3-rc2 API audio processing pipeline.

    Args:
        job_service: For updating job state.
        repo: SQLite repository.
        mvsep: MVSEP API client for stem separation.
        whisper: OpenAI Whisper API client for ASR.
        vad: Voice activity detector.
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
        repo: SQLiteRepository,
        mvsep: MVSEPClient,
        whisper: WhisperAPIClient,
        vad: VADProcessor,
        lyrics_searcher: LyricsAgent | None,
        ctc_aligner: CTCAligner,
        feature_extractor: object | None = None,
        lyric_embedder: object | None = None,
        qdrant_repo: QDrantRepository | None = None,
        settings: object | None = None,
    ) -> None:
        self.job_service = job_service
        self.repo = repo
        self.mvsep = mvsep
        self.whisper = whisper
        self.vad = vad
        self.lyrics_searcher = lyrics_searcher
        self.ctc_aligner = ctc_aligner
        self.feature_extractor = feature_extractor
        self.lyric_embedder = lyric_embedder
        self.qdrant_repo = qdrant_repo
        self.settings = settings

    async def process(self, job: Job) -> None:
        """Run the full API pipeline for a single job."""
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

        vocals_path: str | None = None

        try:
            # ==============================================================
            # STEP 1: MVSEP API separation (~2-3 min)
            # ==============================================================
            await self.job_service.mark_step(job.id, "separating", 0)

            stem_result = await self.mvsep.separate(track.mp3_path)
            vocals_path = stem_result.vocals_path
            instrumental_path = stem_result.instrumental_path

            await self.repo.update_track(
                job.track_id,
                TrackUpdate(instrumental_path=instrumental_path, status="processing"),
            )
            await self.job_service.mark_step(job.id, "separating", 100)

            # Record MVSEP cost.
            audio_duration = await asyncio.to_thread(
                self._get_audio_duration, track.mp3_path,
            )
            await self._record_cost(
                job.track_id, "mvsep", _MVSEP_COST_PER_TRACK,
                duration_sec=audio_duration,
            )

            # ==============================================================
            # PARALLEL: Branch A (VAD + ASR) + Branch B (features)
            # ==============================================================
            feature_result, whisper_result = await asyncio.gather(
                self._extract_features(track.mp3_path, job.id),
                self._vad_and_transcribe(vocals_path, job),
                return_exceptions=True,
            )

            # Feature extraction failure is non-critical.
            feature_vector = None
            if isinstance(feature_result, Exception):
                logger.warning("feature_extraction_failed", error=str(feature_result))
            else:
                feature_vector = feature_result

            # ASR failure is critical.
            if isinstance(whisper_result, Exception):
                raise whisper_result

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
                        job.track_id, track, feature_vector,
                    )
                await self.job_service.mark_permanently_failed(
                    job.id, f"Lyrics search failed: {exc}"
                )
                return

            await self.job_service.mark_step(job.id, "searching_lyrics", 100)

            # Record lyrics agent cost.
            await self._record_cost(job.track_id, "deepseek_chat", _CHAT_COST_ESTIMATE)

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
            # STEPS 6+7: CTC alignment (~22s CPU)
            # ==============================================================
            await self.job_service.mark_step(job.id, "aligning", 0)

            syllable_timings, align_stats = await asyncio.to_thread(
                self.ctc_aligner.align,
                vocals_path,
                lyrics_result.lyrics,
                lyrics_result.language,
            )

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
                detect_line_breaks, syllable_timings, vocals_path,
            )

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
                    self.lyric_embedder.embed, lyrics_result.lyrics,
                )
                await self.job_service.mark_step(job.id, "embedding_lyrics", 100)

            # ==============================================================
            # STEP 10: QDrant sync
            # ==============================================================
            await self._sync_qdrant(
                job.id, job.track_id, track, feature_vector, lyric_vector,
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

            # Remove the original upload — instrumental is sufficient.
            if track.mp3_path:
                original = Path(track.mp3_path)
                if original.exists():
                    original.unlink()
                    logger.info("original_mp3_deleted", path=str(original))

        except Exception as exc:
            logger.error("pipeline_failed", job_id=job.id, error=str(exc), exc_info=True)
            await self.job_service.mark_failed(job.id, str(exc))

        finally:
            # Clean up vocal files.
            if vocals_path:
                Path(vocals_path).unlink(missing_ok=True)
                cleaned = Path(vocals_path).parent / "cleaned_vocals.wav"
                cleaned.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    async def _extract_features(
        self, mp3_path: str, job_id: str,
    ) -> list[float] | None:
        """Extract audio features (runs in parallel with ASR)."""
        if self.feature_extractor is None:
            return None
        await self.job_service.mark_step(job_id, "extracting_features", 0)
        try:
            result = await asyncio.to_thread(
                self.feature_extractor.extract, mp3_path,
            )
            await self.job_service.mark_step(job_id, "extracting_features", 100)
            return result
        except Exception as exc:
            logger.warning("feature_extraction_failed", error=str(exc))
            return None

    async def _vad_and_transcribe(self, vocals_path: str, job: Job):
        """VAD + Whisper API ASR (sequential within the parallel branch)."""
        await self.job_service.mark_step(job.id, "transcribing", 0)

        cleaned_path = await asyncio.to_thread(
            self.vad.process, vocals_path,
        )

        try:
            result = await self.whisper.transcribe(cleaned_path)

            # Record Whisper cost.
            vad_duration = await asyncio.to_thread(
                self._get_audio_duration, cleaned_path,
            )
            whisper_cost = (vad_duration / 60.0) * _WHISPER_COST_PER_MINUTE
            await self._record_cost(
                job.track_id, "openai_whisper", whisper_cost,
                duration_sec=vad_duration,
            )

            await self.job_service.mark_step(job.id, "transcribing", 100)
            return result

        finally:
            # Clean up VAD-processed file.
            if cleaned_path != vocals_path:
                Path(cleaned_path).unlink(missing_ok=True)

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

    async def _record_cost(
        self,
        track_id: str,
        service: str,
        cost_usd: float,
        tokens: int | None = None,
        duration_sec: float | None = None,
    ) -> None:
        """Record API cost, ignoring errors (non-critical)."""
        try:
            await self.repo.record_api_cost(
                track_id=track_id,
                service=service,
                cost_usd=cost_usd,
                tokens=tokens,
                duration_sec=duration_sec,
            )
        except Exception as exc:
            logger.warning("cost_tracking_failed", service=service, error=str(exc))

    @staticmethod
    def _get_audio_duration(audio_path: str) -> float:
        """Get audio duration in seconds using librosa."""
        try:
            import librosa
            return librosa.get_duration(path=audio_path)
        except Exception:
            return 0.0

    @staticmethod
    def _parse_hints_from_path(mp3_path: str) -> tuple[str | None, str | None]:
        """Extract artist/title from filename like 'Artist - Title.mp3'."""
        name = Path(mp3_path).stem
        if " - " in name:
            parts = name.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return None, None
