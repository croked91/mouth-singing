"""GPU audio processing pipeline.

Orchestrates the 7-step pipeline using local GPU hardware:
  1. UVR separation (GPU, BS-Roformer)
  2. VAD on vocals (CPU)
  3. Whisper ASR (GPU, after VAD)
  4. LLM lyrics search (API)
  5+6. CTC alignment (GPU, torchaudio MMS_FA)
  7. Line break detection (CPU)

Feature extraction, lyric embedding, and QDrant sync are handled by the
separate Rec Service, triggered via a RabbitMQ message at finalization.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import structlog

from karaoke_shared.messaging.rabbitmq import RabbitMQClient
from karaoke_shared.models.job import Job
from karaoke_shared.models.track import TrackCreate
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.services.job_service import JobService
from karaoke_shared.storage import S3Storage

from worker.common.base_pipeline import BasePipeline
from worker.common.ctc_aligner import CTCAligner
from worker.common.lyrics import LyricsProviderChain
from worker.common.lyrics_searcher import LyricsSearchError
from worker.common.vad_processor import VADProcessor
from worker.gpu.uvr_separator import UVRSeparator
from worker.gpu.whisper_transcriber import WhisperTranscriber

logger = structlog.get_logger(__name__)


class GpuPipeline(BasePipeline):
    """Orchestrates the GPU audio processing pipeline.

    Args:
        job_service: For updating job state.
        uvr: Vocal/instrumental separator (local GPU).
        repo: PostgreSQL repository.
        whisper: faster-whisper ASR transcriber.
        vad_processor: Voice activity detector.
        lyrics_searcher: Agent-based lyrics finder (optional).
        ctc_aligner: CTC forced alignment.
        storage: S3-compatible object storage.
        rmq: RabbitMQ client for publishing to Rec Service.
        settings: Worker settings instance.
    """

    def __init__(
        self,
        job_service: JobService,
        uvr: UVRSeparator,
        repo: PgRepository,
        whisper: WhisperTranscriber,
        vad_processor: VADProcessor,
        lyrics_searcher: LyricsProviderChain | None,
        ctc_aligner: CTCAligner,
        storage: S3Storage,
        rmq: RabbitMQClient,
        settings: object | None = None,
    ) -> None:
        self.job_service = job_service
        self.uvr = uvr
        self.repo = repo
        self.whisper = whisper
        self.vad_processor = vad_processor
        self.lyrics_searcher = lyrics_searcher
        self.ctc_aligner = ctc_aligner
        self.storage = storage
        self.rmq = rmq
        self.settings = settings

    async def process(self, job: Job) -> None:
        """Run the full GPU pipeline for a single job."""
        if not job.mp3_key:
            await self.job_service.mark_failed(job.id, f"Job {job.id} has no mp3_key")
            return

        try:
            pipeline_t0 = time.monotonic()

            # Download the original MP3 from S3 to a temp file.
            local_mp3 = f"/tmp/{job.id}.mp3"
            await self.storage.download_to_file(job.mp3_key, local_mp3)

            # ==============================================================
            # STEP 1: UVR separation (GPU, ~60-90s)
            # ==============================================================
            await self.job_service.mark_step(job.id, "separating", 0)

            vocals_path, instrumental_path = await self._separate_with_fallback(
                local_mp3
            )

            # Free UVR VRAM.
            await asyncio.to_thread(self.uvr.cleanup)

            # Launch instrumental encode + upload in background.
            instrumental_key = f"instrumentals/{job.id}.mp3"
            instrumental_upload_task = asyncio.create_task(
                self._encode_and_upload_instrumental(
                    instrumental_path,
                    instrumental_key,
                    job.id,
                    local_mp3,
                )
            )

            await self.job_service.mark_step(job.id, "separating", 100)

            # ==============================================================
            # STEPS 2+3: VAD + ASR (sequential, runs while upload proceeds)
            # ==============================================================
            whisper_result, vad_segments = await self._vad_and_transcribe(
                vocals_path,
                job.id,
            )

            # Free Whisper VRAM.
            await asyncio.to_thread(self.whisper.cleanup)

            # ==============================================================
            # STEP 4: LLM lyrics search (~2-5s)
            # ==============================================================
            await self.job_service.mark_step(job.id, "searching_lyrics", 0)

            if self.lyrics_searcher is None:
                await self.job_service.mark_permanently_failed(
                    job.id,
                    "Lyrics agent not configured (check DEEPSEEK/YANDEX env vars)",
                )
                return

            artist_hint = job.artist_hint
            title_hint = job.title_hint
            filename = (job.data or {}).get("filename")

            try:
                lyrics_result = await self.lyrics_searcher.search(
                    asr_text=whisper_result.text,
                    detected_language=whisper_result.language,
                    artist_hint=artist_hint,
                    title_hint=title_hint,
                    filename=filename,
                )
            except LyricsSearchError as exc:
                logger.error("lyrics_search_failed", job_id=job.id, error=str(exc))
                await self.job_service.mark_permanently_failed(
                    job.id, f"Lyrics search failed: {exc}"
                )
                return

            await self.job_service.mark_step(job.id, "searching_lyrics", 100)

            # Store lyrics result in job data for finalization.
            await self.repo.update_job_data(
                job.id,
                {
                    "artist": lyrics_result.artist,
                    "title": lyrics_result.title,
                    "lyrics": lyrics_result.lyrics,
                    "language": lyrics_result.language,
                },
            )

            # ==============================================================
            # STEPS 5+6: CTC alignment (GPU via torchaudio)
            # ==============================================================
            syllable_timings, align_stats = await asyncio.to_thread(
                self.ctc_aligner.align,
                vocals_path,
                lyrics_result.lyrics,
                lyrics_result.language,
            )

            # Free CTC model VRAM.
            if hasattr(self.ctc_aligner, "cleanup"):
                await asyncio.to_thread(self.ctc_aligner.cleanup)
            logger.info(
                "ctc_alignment_done",
                job_id=job.id,
                total_words=align_stats.total_words,
                char_level=align_stats.char_level_used,
                fallback=align_stats.proportional_fallback,
            )

            # ==============================================================
            # STEP 7: Line break detection (CPU, fast)
            # ==============================================================
            from karaoke_shared.utils.line_breaker import detect_line_breaks

            syllable_timings = await asyncio.to_thread(
                detect_line_breaks, syllable_timings, vocals_path
            )

            # Clean up vocal files after line break detection.
            Path(vocals_path).unlink(missing_ok=True)
            track_id_stem = Path(vocals_path).stem.split("_")[0]
            cleaned_path = (
                Path(vocals_path).parent / f"cleaned_vocals_{track_id_stem}.wav"
            )
            cleaned_path.unlink(missing_ok=True)

            # ==============================================================
            # FINALIZATION: create track, publish to Rec Service
            # ==============================================================

            # Wait for instrumental upload to finish before creating track.
            await instrumental_upload_task

            # Gather all data from job_queue.data JSONB.
            updated_job = await self.repo.get_job(job.id)
            job_data = updated_job.data or {} if updated_job else {}

            track = await self.repo.create_track(
                TrackCreate(
                    artist=lyrics_result.artist,
                    title=lyrics_result.title,
                    source="user_upload",
                    instrumental_key=job_data.get("instrumental_key", instrumental_key),
                    lyrics_text=lyrics_result.lyrics,
                    syllable_timings=syllable_timings,
                    language=lyrics_result.language,
                    status="ready",
                    qdrant_synced=0,
                )
            )
            track_id = track.id

            await self.repo.set_job_track_id(job.id, track_id)

            await self.job_service.mark_completed(
                job.id,
                {
                    "track_id": track_id,
                    "instrumental_key": job_data.get(
                        "instrumental_key", instrumental_key
                    ),
                    "language": lyrics_result.language,
                },
            )

            # Publish to Rec Service for feature extraction + embedding + QDrant sync.
            await self.rmq.publish(
                "rec",
                "",
                {
                    "track_id": track_id,
                    "mp3_key": job.mp3_key,
                    "lyrics": lyrics_result.lyrics,
                },
            )

            logger.info(
                "pipeline_completed",
                job_id=job.id,
                track_id=track_id,
                total_duration_sec=round(time.monotonic() - pipeline_t0, 2),
            )

            # Clean up temp files.
            Path(local_mp3).unlink(missing_ok=True)
            Path(instrumental_path).unlink(missing_ok=True)

        except Exception as exc:
            logger.error(
                "pipeline_failed", job_id=job.id, error=str(exc), exc_info=True
            )
            # Release GPU VRAM to prevent OOM on subsequent jobs.
            try:
                await asyncio.to_thread(self.cleanup)
            except Exception:
                pass
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

    async def _encode_and_upload_instrumental(
        self,
        instrumental_path: str,
        instrumental_key: str,
        job_id: str,
        original_mp3: str,
    ) -> None:
        """Convert instrumental WAV→MP3 and upload to S3 (runs in background)."""
        # Detect original bitrate to preserve quality.
        bitrate = "192k"
        try:
            probe = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format=bit_rate",
                "-of",
                "csv=p=0",
                original_mp3,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await probe.communicate()
            orig_bps = int(stdout.decode().strip())
            bitrate = f"{orig_bps // 1000}k"
        except Exception:
            pass

        instrumental_mp3 = f"/tmp/{job_id}_instrumental.mp3"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            instrumental_path,
            "-codec:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            instrumental_mp3,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        with open(instrumental_mp3, "rb") as f:
            await self.storage.upload(instrumental_key, f.read())
        Path(instrumental_mp3).unlink(missing_ok=True)
        await self.repo.update_job_data(job_id, {"instrumental_key": instrumental_key})

    async def _separate_with_fallback(self, mp3_path: str) -> tuple[str, str]:
        """Try GPU separation, fall back to CPU on OOM."""
        try:
            return await asyncio.to_thread(self.uvr.separate, mp3_path)
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower() or "cuda" in str(exc).lower():
                logger.warning("uvr_cuda_oom_fallback", error=str(exc))
                await asyncio.to_thread(self.uvr.cleanup)
                self.uvr = UVRSeparator(
                    model_cache_dir=self.uvr.model_cache_dir,
                    media_root=self.uvr.media_root,
                    model_name=self.uvr._model_name,
                    torch_device="cpu",
                    chunk_batch_size=1,
                    use_autocast=False,
                    overlap=self.uvr._overlap,
                )
                return await asyncio.to_thread(self.uvr.separate, mp3_path)
            raise

    async def _vad_and_transcribe(
        self,
        vocals_path: str,
        job_id: str,
    ):
        """VAD + Whisper ASR (sequential).

        Whisper runs on the ORIGINAL vocals (not VAD-cleaned) so that
        word timestamps are in original audio time — no projection needed.
        VAD-cleaned audio is no longer used for ASR.

        Returns:
            (WhisperResult, vad_segments) where vad_segments is a list of
            (start_sec, end_sec) voiced intervals in original audio time.
        """
        vad_result = await asyncio.to_thread(
            self.vad_processor.process,
            vocals_path,
        )

        await self.job_service.mark_step(job_id, "transcribing", 0)

        result = await asyncio.to_thread(
            self.whisper.transcribe,
            vad_result.cleaned_path,
        )

        await self.job_service.mark_step(job_id, "transcribing", 100)
        return result, vad_result.segments

    @staticmethod
    def _parse_hints_from_path(mp3_path: str) -> tuple[str | None, str | None]:
        """Extract artist/title from filename like 'Artist - Title.mp3'."""
        name = Path(mp3_path).stem
        if " - " in name:
            parts = name.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return None, None
