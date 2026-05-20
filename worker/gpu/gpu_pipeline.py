"""GPU audio processing pipeline.

Orchestrates the 7-step pipeline using local GPU hardware. All seven steps
run sequentially; only the instrumental WAV→MP3 encode + S3 upload runs as
a background asyncio task in parallel with steps 2..7.

  1.  separating              — UVR (BS-Roformer) vocals/instrumental split
  2.  back_vocal_separating   — Mel-Band RoFormer aufr33 lead/backing split
  3.  VAD on FULL vocals (CPU) — backing vocals help Whisper recognise the track
  4.  transcribing            — Whisper ASR on VAD-cleaned FULL vocals
  5.  searching_lyrics        — provider chain / lyrics agent
  6.  CTC alignment on LEAD vocals (torchaudio MMS_FA)
  7.  Line break detection (CPU)

Feature extraction, lyric embedding, and QDrant sync are handled by the
separate Rec Service, triggered via a RabbitMQ message at finalization.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from structlog.contextvars import get_contextvars

from karaoke_shared.alignment import syllable_timings_to_document
from karaoke_shared.messaging.rabbitmq import RabbitMQClient
from karaoke_shared.models.alignment import AlignmentRevision
from karaoke_shared.models.job import Job
from karaoke_shared.models.track import TrackCreate
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.services.job_service import JobService
from karaoke_shared.storage import S3Storage

from worker.common.base_pipeline import BasePipeline
from worker.common.lyrics import LyricsProviderChain
from worker.common.vad_processor import VADProcessor
from worker.gpu.torch_ctc_aligner import TorchCTCAligner
from worker.gpu.uvr_separator import UVRSeparator
from worker.gpu.whisper_transcriber import WhisperTranscriber

if TYPE_CHECKING:
    from worker.gpu.back_vocal_separator import BackVocalSeparator

logger = structlog.get_logger(__name__)


class GpuPipeline(BasePipeline):
    """Orchestrates the GPU audio processing pipeline.

    Args:
        job_service: For updating job state.
        uvr: Vocal/instrumental separator (local GPU).
        repo: PostgreSQL repository.
        whisper: Whisper ASR transcriber (HuggingFace Transformers).
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
        ctc_aligner: TorchCTCAligner,
        storage: S3Storage,
        rmq: RabbitMQClient,
        settings: object | None = None,
        back_vocal_separator: "BackVocalSeparator | None" = None,
    ) -> None:
        self.job_service = job_service
        self.uvr = uvr
        self.back_vocal_separator = back_vocal_separator
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
        task = (job.data or {}).get("task")
        if task == "alignment_auto_repair":
            from worker.common.alignment_auto_repair import AlignmentAutoRepairEngine

            engine = AlignmentAutoRepairEngine(
                job_service=self.job_service,
                repo=self.repo,
                storage=self.storage,
                vad_processor=self.vad_processor,
                ctc_aligner=self.ctc_aligner,
            )
            await engine.process(job)
            return

        if not job.mp3_key:
            await self.job_service.mark_permanently_failed(
                job.id, f"Job {job.id} has no mp3_key"
            )
            return

        # Tempfile paths and background task captured here so the finally
        # block can clean them up unconditionally on success or failure.
        local_mp3: str | None = None
        vocals_path: str | None = None
        instrumental_path: str | None = None
        lead_vocals_path: str | None = None
        backing_path: str | None = None
        cleaned_vocals_path: str | None = None
        instrumental_upload_task: asyncio.Task | None = None

        try:
            pipeline_t0 = time.monotonic()

            # Download the original MP3 from S3 to a temp file.
            local_mp3 = f"/tmp/{job.id}.mp3"
            await self.storage.download_to_file(job.mp3_key, local_mp3)

            # Probe duration once → scale per-step timeouts proportionally.
            duration_sec = await self._probe_duration_seconds(local_mp3)
            baseline = self.settings.step_timeout_baseline_seconds
            scale = max(duration_sec / baseline, 0.5)
            logger.info(
                "step_timeouts_calculated",
                job_id=job.id,
                duration_sec=round(duration_sec, 1),
                scale=round(scale, 2),
            )

            # ==============================================================
            # STEP 1: UVR separation (GPU)
            # NOTE: asyncio.wait_for(asyncio.to_thread(...)) cancels only
            # the awaiting coroutine; the underlying GPU thread keeps
            # running until the CUDA kernel returns. A hard external
            # watchdog (separate process / k8s liveness) is out of scope.
            # ==============================================================
            await self.job_service.mark_step(job.id, "separating", 0)
            sep_timeout = self.settings.step_timeout_separating_base * scale
            try:
                vocals_path, instrumental_path = await asyncio.wait_for(
                    self._separate_with_fallback(local_mp3),
                    timeout=sep_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"Step timeout: separating after {sep_timeout:.1f}s "
                    f"(duration={duration_sec:.1f}s, scale={scale:.2f})"
                ) from exc

            # Free UVR VRAM.
            await asyncio.to_thread(self.uvr.cleanup)

            await self.job_service.mark_step(job.id, "separating", 100)

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

            # ==============================================================
            # STEP 2: Back-vocal separation (lead vs backing vocals)
            # ==============================================================
            lead_vocals_path = vocals_path
            if self.back_vocal_separator is not None:
                await self.job_service.mark_step(
                    job.id, "back_vocal_separating", 0
                )
                bvs_timeout = (
                    self.settings.step_timeout_back_vocal_separating_base * scale
                )
                try:
                    lead_vocals_path, backing_path = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.back_vocal_separator.separate, vocals_path
                        ),
                        timeout=bvs_timeout,
                    )
                    logger.info(
                        "back_vocal_separation_done",
                        job_id=job.id,
                        lead_vocals_path=lead_vocals_path,
                    )
                except asyncio.TimeoutError:
                    # Fall back to full vocals — backing harmonies in the
                    # lead are better than failing the whole job on this
                    # optional step.
                    logger.warning(
                        "back_vocal_separation_timeout_falling_back_to_full_vocals",
                        job_id=job.id,
                        timeout_sec=bvs_timeout,
                    )
                    lead_vocals_path = vocals_path
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "back_vocal_separation_failed_falling_back_to_full_vocals",
                        job_id=job.id,
                        error=str(exc),
                    )
                    lead_vocals_path = vocals_path
                finally:
                    await asyncio.to_thread(self.back_vocal_separator.cleanup)
                await self.job_service.mark_step(
                    job.id, "back_vocal_separating", 100
                )

            review_vocal_key = f"review-vocals/{job.id}.mp3"
            review_vocal_upload_task = asyncio.create_task(
                self._encode_and_upload_review_vocal(
                    lead_vocals_path,
                    review_vocal_key,
                    job.id,
                )
            )

            # ==============================================================
            # STEP 3: VAD — RMS-based silence removal on FULL vocals.
            # Backing vocals are kept on purpose: they help Whisper
            # recognise the track. Lead-only was tried and produced a
            # ~44% shorter transcript → lyrics matcher picked the wrong
            # song version.
            # ==============================================================
            cleaned_vocals_path = await self._vad(vocals_path, job.id)

            # ==============================================================
            # STEP 4: Whisper ASR on the VAD-cleaned FULL vocals.
            # Runs while the background instrumental upload proceeds.
            # ==============================================================
            wsp_timeout = self.settings.step_timeout_transcribing_base * scale
            try:
                whisper_result = await asyncio.wait_for(
                    self._transcribe(cleaned_vocals_path, job.id),
                    timeout=wsp_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"Step timeout: transcribing after {wsp_timeout:.1f}s "
                    f"(duration={duration_sec:.1f}s, scale={scale:.2f})"
                ) from exc

            # Free Whisper VRAM.
            await asyncio.to_thread(self.whisper.cleanup)

            # ==============================================================
            # STEP 5: LLM lyrics search
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

            # LyricsSearchError (and any other failure here) propagates to the
            # outer except, which logs `pipeline_failed` once and writes the
            # message via mark_permanently_failed. Catching it locally would
            # double-log the same incident.
            lyrics_result = await self.lyrics_searcher.search(
                asr_text=whisper_result.text,
                detected_language=whisper_result.language,
                artist_hint=artist_hint,
                title_hint=title_hint,
                filename=filename,
            )

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
            # STEP 6: CTC alignment (GPU via torchaudio)
            # ==============================================================
            await self.job_service.mark_step(job.id, "aligning", 0)
            ctc_timeout = self.settings.step_timeout_aligning_base * scale
            try:
                syllable_timings, align_stats = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.ctc_aligner.align,
                        lead_vocals_path,
                        lyrics_result.lyrics,
                        lyrics_result.language,
                    ),
                    timeout=ctc_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"Step timeout: aligning after {ctc_timeout:.1f}s "
                    f"(duration={duration_sec:.1f}s, scale={scale:.2f})"
                ) from exc
            await self.job_service.mark_step(job.id, "aligning", 100)

            # Free CTC model VRAM.
            if hasattr(self.ctc_aligner, "cleanup"):
                await asyncio.to_thread(self.ctc_aligner.cleanup)
            logger.info(
                "ctc_alignment_done",
                job_id=job.id,
                total_words=align_stats.total_words,
                fallback=align_stats.proportional_fallback,
            )

            # ==============================================================
            # STEP 7: Line break detection (CPU, fast)
            # ==============================================================
            from karaoke_shared.utils.line_breaker import detect_line_breaks

            await self.job_service.mark_step(job.id, "line_breaking", 0)
            syllable_timings = await asyncio.to_thread(
                detect_line_breaks, syllable_timings, lead_vocals_path
            )
            await self.job_service.mark_step(job.id, "line_breaking", 100)

            # If back_vocal_separator returned only the lead path, derive
            # the backing path from naming convention so finally cleanup
            # removes both stems.
            if backing_path is None and lead_vocals_path != vocals_path:
                backing_path = str(
                    Path(lead_vocals_path).with_name(
                        Path(lead_vocals_path)
                        .name.replace("_(Lead).wav", "_(Backing).wav")
                    )
                )

            # ==============================================================
            # FINALIZATION: create track, publish to Rec Service
            # ==============================================================

            # Wait for stem uploads to finish before creating track.
            await instrumental_upload_task
            await review_vocal_upload_task

            # Gather all data from job_queue.data JSONB.
            updated_job = await self.repo.get_job(job.id)
            job_data = updated_job.data or {} if updated_job else {}

            track = await self.repo.create_track(
                TrackCreate(
                    artist=lyrics_result.artist,
                    title=lyrics_result.title,
                    source="user_upload",
                    instrumental_key=job_data.get("instrumental_key", instrumental_key),
                    review_vocal_key=job_data.get("review_vocal_key", review_vocal_key),
                    lyrics_text=lyrics_result.lyrics,
                    lyrics_source=lyrics_result.source_note,
                    syllable_timings=syllable_timings,
                    language=lyrics_result.language,
                    status="ready",
                )
            )
            track_id = track.id

            await self.repo.create_alignment_revision(
                AlignmentRevision(
                    track_id=track_id,
                    revision_no=1,
                    source="auto",
                    lyrics_text=lyrics_result.lyrics,
                    syllable_timings=syllable_timings,
                    document=syllable_timings_to_document(syllable_timings),
                    is_published=True,
                    published_at=track.updated_at,
                )
            )

            await self.repo.set_job_track_id(job.id, track_id)

            await self.job_service.mark_completed(
                job.id,
                {
                    "track_id": track_id,
                    "instrumental_key": job_data.get(
                        "instrumental_key", instrumental_key
                    ),
                    "review_vocal_key": job_data.get(
                        "review_vocal_key", review_vocal_key
                    ),
                    "language": lyrics_result.language,
                },
            )

            # Publish to Rec Service for feature extraction + embedding + QDrant sync.
            # Carry request_id from contextvars so rec-service logs can be
            # stitched to the original upload too.
            rec_body: dict = {
                "track_id": track_id,
                "mp3_key": job.mp3_key,
                "lyrics": lyrics_result.lyrics,
            }
            request_id = get_contextvars().get("request_id")
            if request_id:
                rec_body["request_id"] = request_id
            await self.rmq.publish("rec", "", rec_body)

            logger.info(
                "pipeline_completed",
                job_id=job.id,
                track_id=track_id,
                total_duration_sec=round(time.monotonic() - pipeline_t0, 2),
            )

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
        finally:
            # Cancel background instrumental upload if pipeline aborted —
            # avoids orphan S3 objects and stray disk usage for tracks
            # that will never be finalised.
            if (
                instrumental_upload_task is not None
                and not instrumental_upload_task.done()
            ):
                instrumental_upload_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await instrumental_upload_task
            # Remove all tempfiles created during the pipeline. Each path
            # stays None until the producing step actually ran, so we never
            # try to delete a file that doesn't exist.
            cleanup_paths = [
                local_mp3,
                vocals_path,
                instrumental_path,
                cleaned_vocals_path,
            ]
            if lead_vocals_path and lead_vocals_path != vocals_path:
                cleanup_paths.append(lead_vocals_path)
            cleanup_paths.append(backing_path)
            for path in cleanup_paths:
                if path:
                    with suppress(Exception):
                        Path(path).unlink(missing_ok=True)

    def cleanup(self) -> None:
        """Release GPU resources (UVR + BackVocal + Whisper + CTC models)."""
        self.uvr.cleanup()
        if self.back_vocal_separator is not None:
            self.back_vocal_separator.cleanup()
        self.whisper.cleanup()
        if hasattr(self.ctc_aligner, "cleanup"):
            self.ctc_aligner.cleanup()

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    async def _ffprobe_field(self, mp3_path: str, entries: str) -> str | None:
        """Return a single ffprobe field value, or None on failure.

        ``entries`` is passed to ``-show_entries`` (e.g. ``"format=duration"``,
        ``"format=bit_rate"``). Logs ffprobe stderr on non-zero exit so
        diagnostics survive instead of being silently swallowed.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v", "error",
                "-show_entries", entries,
                "-of", "default=noprint_wrappers=1:nokey=1",
                mp3_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    "ffprobe_failed",
                    mp3_path=mp3_path,
                    entries=entries,
                    returncode=proc.returncode,
                    stderr=stderr.decode("utf-8", errors="replace")[:200],
                )
                return None
            value = stdout.decode("utf-8", errors="replace").strip()
            return value or None
        except Exception as exc:
            logger.warning(
                "ffprobe_exec_failed",
                mp3_path=mp3_path,
                entries=entries,
                error=str(exc),
            )
            return None

    async def _probe_duration_seconds(self, mp3_path: str) -> float:
        """Probe mp3 duration via ffprobe; fallback to baseline on failure."""
        baseline = self.settings.step_timeout_baseline_seconds
        value = await self._ffprobe_field(mp3_path, "format=duration")
        if value is None:
            return baseline
        try:
            return float(value)
        except ValueError:
            logger.warning("ffprobe_duration_unparseable", value=value)
            return baseline

    async def _encode_and_upload_instrumental(
        self,
        instrumental_path: str,
        instrumental_key: str,
        job_id: str,
        original_mp3: str,
    ) -> None:
        """Convert instrumental WAV→MP3 and upload to S3 (runs in background).

        On asyncio.CancelledError (pipeline aborted before finalisation) the
        upload is skipped and the local mp3 is removed, so failed jobs leave
        no orphan ``instrumentals/{job_id}.mp3`` in S3.
        """
        instrumental_mp3 = f"/tmp/{job_id}_instrumental.mp3"
        try:
            # Detect original bitrate to preserve quality; ffprobe failure
            # falls back to a sensible default.
            bitrate = "192k"
            value = await self._ffprobe_field(original_mp3, "format=bit_rate")
            if value is not None:
                try:
                    bitrate = f"{int(value) // 1000}k"
                except ValueError:
                    logger.warning("ffprobe_bitrate_unparseable", value=value)

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
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    "ffmpeg encode failed (rc="
                    f"{proc.returncode}): "
                    f"{stderr.decode('utf-8', errors='replace')[:500]}"
                )

            with open(instrumental_mp3, "rb") as f:
                await self.storage.upload(instrumental_key, f.read())
            await self.repo.update_job_data(
                job_id, {"instrumental_key": instrumental_key}
            )
        except asyncio.CancelledError:
            logger.info("instrumental_upload_cancelled", job_id=job_id)
            raise
        finally:
            with suppress(Exception):
                Path(instrumental_mp3).unlink(missing_ok=True)

    async def _encode_and_upload_review_vocal(
        self,
        vocal_path: str,
        review_vocal_key: str,
        job_id: str,
    ) -> None:
        """Convert the review vocal stem to MP3 and upload it for editor jobs."""
        review_vocal_mp3 = f"/tmp/{job_id}_review_vocal.mp3"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            vocal_path,
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "160k",
            review_vocal_mp3,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("Failed to encode review vocal stem")

        with open(review_vocal_mp3, "rb") as f:
            await self.storage.upload(review_vocal_key, f.read())
        Path(review_vocal_mp3).unlink(missing_ok=True)
        await self.repo.update_job_data(job_id, {"review_vocal_key": review_vocal_key})

    async def _separate_with_fallback(self, mp3_path: str) -> tuple[str, str]:
        """Try GPU separation; on ANY RuntimeError retry once on GPU, then CPU.

        Strategy (covers OOM, cuDNN/cuBLAS hiccups, device-side asserts,
        transient driver glitches — all of which surface as RuntimeError):

        1. First attempt on the configured (GPU) device.
        2. On RuntimeError → cleanup (frees VRAM, drops model) → one retry
           on the same GPU. Catches transient failures (other process
           briefly held VRAM, driver hiccup) without paying the CPU cost.
        3. If the retry also raises RuntimeError → cleanup → CPU fallback.
        4. If CPU also raises → propagate (pipeline marks failed → DLQ).
        """
        try:
            return await asyncio.to_thread(self.uvr.separate, mp3_path)
        except RuntimeError as first_exc:
            logger.warning(
                "uvr_gpu_failure_retrying_on_gpu",
                error=str(first_exc),
            )
            await asyncio.to_thread(self.uvr.cleanup)
            try:
                return await asyncio.to_thread(self.uvr.separate, mp3_path)
            except RuntimeError as retry_exc:
                logger.warning(
                    "uvr_gpu_retry_failed_falling_back_to_cpu",
                    first_error=str(first_exc),
                    retry_error=str(retry_exc),
                )
                await asyncio.to_thread(self.uvr.cleanup)
                self.uvr = self.uvr.fallback_to_cpu()
                return await asyncio.to_thread(self.uvr.separate, mp3_path)

    async def _vad(self, vocals_path: str, job_id: str) -> str:
        """STEP 3: VAD — strip silence from the full vocals track.

        Emits ``mark_step("vad", 0)`` before the call and
        ``mark_step("vad", 100)`` after it returns. Returns the path
        to the cleaned (voiced-only) audio file that Whisper will then
        transcribe.
        """
        await self.job_service.mark_step(job_id, "vad", 0)
        vad_result = await asyncio.to_thread(
            self.vad_processor.process,
            vocals_path,
        )
        await self.job_service.mark_step(job_id, "vad", 100)
        return vad_result.cleaned_path

    async def _transcribe(self, cleaned_vocals_path: str, job_id: str):
        """STEP 4: Whisper ASR on the VAD-cleaned vocals.

        Emits ``mark_step("transcribing", 0)`` before the call and
        ``mark_step("transcribing", 100)`` after it returns.
        """
        await self.job_service.mark_step(job_id, "transcribing", 0)
        result = await asyncio.to_thread(
            self.whisper.transcribe,
            cleaned_vocals_path,
        )
        await self.job_service.mark_step(job_id, "transcribing", 100)
        return result
