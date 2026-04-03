"""Bootstrap pipeline — bulk catalog import without Whisper/LLM/VAD.

Simplified GPU pipeline for tracks with pre-existing lyrics.
Skips: Whisper ASR, LLM lyrics search, VAD, line break detection.
CTC receives raw vocals (same as GpuPipeline).
"""

from __future__ import annotations

import asyncio
import re
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


def _clean_lyrics_for_ctc(text: str) -> str:
    """Clean lyrics text for CTC forced alignment.

    CTC forced aligner tokenizes text into characters after romanization.
    It fails on: non-breaking spaces, dashes between words, numbers,
    special unicode chars, metadata/credits lines.

    Does NOT use NFKD normalization (breaks Russian ё → е + combining).
    """
    # Non-breaking spaces → regular spaces
    text = text.replace("\xa0", " ")
    # Fancy quotes/apostrophes → simple
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", "").replace("\u201d", "")
    # HTML entities (leftover from scraping)
    text = text.replace("&#x27;", "'").replace("&#39;", "'")
    text = text.replace("&amp;", "and")

    lines = text.splitlines()
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()

        # Keep empty lines as paragraph breaks
        if not stripped:
            cleaned_lines.append("")
            continue

        # Skip metadata/credits lines
        if re.search(
            r"\b(BMI|ASCAP|SESAC|Publishing|Copyright|©|℗|"
            r"Written by|Lyrics by|Composed by|Produced by|"
            r"Music by|Words by|Муз\.|Сл\.|Музыка|Слова|Текст|"
            r"Автор|Подбор|Перевод)\b",
            stripped,
            re.IGNORECASE,
        ):
            continue

        # Skip inline metadata: "(оригинал ...)", "SPOKEN:", etc.
        stripped = re.sub(
            r"\(оригинал[^)]*\)|\(original[^)]*\)", "", stripped, flags=re.IGNORECASE
        ).strip()
        stripped = re.sub(r"^SPOKEN\s*:\s*", "", stripped, flags=re.IGNORECASE).strip()

        if not stripped:
            continue

        # Skip lines that look like credits (mostly non-letters)
        letters = sum(1 for c in stripped if c.isalpha())
        if len(stripped) > 15 and letters / len(stripped) < 0.4:
            continue

        # Skip lines with CJK characters (CTC doesn't support them)
        if re.search(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]", stripped):
            continue

        cleaned_lines.append(stripped)

    text = "\n".join(cleaned_lines)

    # Replace dashes (em, en, regular) with spaces
    text = re.sub(r"[—–\-]", " ", text)

    # Remove numbers (CTC can't align digits)
    text = re.sub(r"\d+", " ", text)

    # Keep only Cyrillic, Latin, spaces, newlines, apostrophes
    # This removes â, ñ, ў and other accented chars that CTC can't tokenize
    text = re.sub(r"[^A-Za-zА-Яа-яЁё \n']", " ", text)

    # Collapse multiple spaces
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse 3+ newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip each line
    text = "\n".join(line.strip() for line in text.splitlines())

    return text.strip()


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
        rec_cluster_assigner: object | None = None,
    ) -> None:
        self.job_service = job_service
        self.uvr = uvr
        self.repo = repo
        self.ctc_aligner = ctc_aligner
        self.feature_extractor = feature_extractor
        self.lyric_embedder = lyric_embedder
        self.qdrant_repo = qdrant_repo
        self.rec_cluster_assigner = rec_cluster_assigner

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

            lyrics_text = _clean_lyrics_for_ctc(track.lyrics_text)
            language = track.language or "ru"

            if not lyrics_text:
                await self.job_service.mark_failed(
                    job.id, "Lyrics empty after cleaning"
                )
                return

            # === STEP 1: UVR Separation (GPU, ~60s) ===
            await self.job_service.mark_step(job.id, "separating", 0)
            vocals_path, instrumental_path = await asyncio.to_thread(
                self.uvr.separate, track.mp3_path
            )
            # No cleanup() here — model stays resident in VRAM.
            # Each container has its own GPU, so no VRAM contention.
            # 4 workers × 1.7 GB = 6.8 GB out of 24 GB.
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

            # === STEP 5: Assign rec_cluster_id + QDrant Sync ===
            rec_cluster_id = None
            if self.rec_cluster_assigner and self.rec_cluster_assigner.available:
                rec_cluster_id = await asyncio.to_thread(
                    self.rec_cluster_assigner.assign, feature_vector, lyric_vector,
                )

            if self.qdrant_repo is not None:
                payload = {
                    "track_id": job.track_id,
                    "artist": track.artist,
                    "title": track.title,
                    "status": "ready",
                }
                if rec_cluster_id is not None:
                    payload["rec_cluster_id"] = rec_cluster_id
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
                TrackUpdate(status="ready", qdrant_synced=1, mp3_path=None, rec_cluster_id=rec_cluster_id),
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
