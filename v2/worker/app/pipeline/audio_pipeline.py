"""Audio processing pipeline.

Orchestrates the full audio processing pipeline for a single job.  Steps:
  - Step 1 — UVR vocal/instrumental separation.
  - Step 2 — Soniox transcription + syllabification.
  - Step 3 — FeatureExtractor (librosa, 45-d).
  - Step 4 — LyricEmbedder (sentence-transformers, 384-d).
  - Step 5 — QDrant sync (audio_features + lyrics_embeddings).
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
from karaoke_shared.utils.syllabifier import Syllabifier

from app.pipeline.sonoix_client import SonoixClient
from app.pipeline.uvr_separator import UVRSeparator

logger = structlog.get_logger(__name__)


class AudioPipeline:
    """Orchestrates the full audio processing pipeline.

    Each pipeline step is implemented as a discrete block within
    :meth:`process`.  Dependencies are injected so that each component can be
    tested or swapped independently.

    ``sonoix``, ``feature_extractor``, ``lyric_embedder``, and
    ``qdrant_repo`` are optional: when ``None`` the corresponding pipeline
    steps are skipped.  This allows unit tests to construct the pipeline
    without providing all dependencies.

    Args:
        job_service: Service for updating job state throughout processing.
        uvr: The vocal/instrumental separator.
        repo: Repository for reading track data and persisting updates.
        sonoix: Soniox Speech-to-Text client for transcription.
        feature_extractor: Extracts 45-d audio feature vector.
        lyric_embedder: Embeds lyrics into 384-d vector.
        qdrant_repo: QDrant repository for upserting vectors.
    """

    def __init__(
        self,
        job_service: JobService,
        uvr: UVRSeparator,
        repo: SQLiteRepository,
        sonoix: SonoixClient | None = None,
        feature_extractor: object | None = None,
        lyric_embedder: object | None = None,
        qdrant_repo: QDrantRepository | None = None,
    ) -> None:
        self.job_service = job_service
        self.uvr = uvr
        self.repo = repo
        self.sonoix = sonoix
        self.feature_extractor = feature_extractor
        self.lyric_embedder = lyric_embedder
        self.qdrant_repo = qdrant_repo
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

                # Inject line breaks from timing gaps / beat detection.
                from karaoke_shared.utils.line_breaker import detect_line_breaks  # noqa: PLC0415

                syllable_timings = await asyncio.to_thread(
                    detect_line_breaks, syllable_timings, vocals_path
                )

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

            # Vocals stem no longer needed — delete to save disk.
            if vocals_path:
                Path(vocals_path).unlink(missing_ok=True)

            # ------------------------------------------------------------------
            # Steps 3+4: Feature extraction & lyric embedding
            # Run in parallel via asyncio.gather.
            # ------------------------------------------------------------------
            feature_vector: list[float] | None = None
            lyric_vector: list[float] | None = None

            tasks = []

            if self.feature_extractor is not None:
                await self.job_service.mark_step(job.id, "extracting_features", 0)

                async def _extract_features() -> list[float]:
                    vec = await asyncio.to_thread(
                        self.feature_extractor.extract, track.mp3_path
                    )
                    await self.job_service.mark_step(job.id, "extracting_features", 100)
                    return vec

                tasks.append(_extract_features())

            if self.lyric_embedder is not None and transcription is not None:
                await self.job_service.mark_step(job.id, "embedding_lyrics", 0)

                async def _embed_lyrics() -> list[float]:
                    vec = await asyncio.to_thread(
                        self.lyric_embedder.embed, transcription.full_text
                    )
                    await self.job_service.mark_step(job.id, "embedding_lyrics", 100)
                    return vec

                tasks.append(_embed_lyrics())

            if tasks:
                results_list = await asyncio.gather(*tasks)
                idx = 0
                if self.feature_extractor is not None:
                    feature_vector = results_list[idx]
                    idx += 1
                if self.lyric_embedder is not None and transcription is not None:
                    lyric_vector = results_list[idx]

                logger.info(
                    "step_completed",
                    job_id=job.id,
                    step="feature_extraction_and_embedding",
                    has_audio_features=feature_vector is not None,
                    has_lyric_embedding=lyric_vector is not None,
                )

            # ------------------------------------------------------------------
            # Step 5: QDrant sync
            # ------------------------------------------------------------------
            qdrant_synced = False

            if self.qdrant_repo is not None:
                await self.job_service.mark_step(job.id, "syncing_qdrant", 0)

                payload = {
                    "track_id": job.track_id,
                    "artist": track.artist,
                    "title": track.title,
                    "status": "ready",
                }

                if feature_vector is not None and any(
                    v != 0.0 for v in feature_vector
                ):
                    await asyncio.to_thread(
                        self.qdrant_repo.upsert,
                        "audio_features",
                        job.track_id,
                        feature_vector,
                        payload,
                    )
                elif feature_vector is not None:
                    logger.warning(
                        "skipping_zero_audio_vector",
                        job_id=job.id,
                        track_id=job.track_id,
                    )

                if lyric_vector is not None and any(
                    v != 0.0 for v in lyric_vector
                ):
                    await asyncio.to_thread(
                        self.qdrant_repo.upsert,
                        "lyrics_embeddings",
                        job.track_id,
                        lyric_vector,
                        payload,
                    )
                elif lyric_vector is not None:
                    logger.warning(
                        "skipping_zero_lyrics_vector",
                        job_id=job.id,
                        track_id=job.track_id,
                    )

                qdrant_synced = feature_vector is not None or lyric_vector is not None

                await self.job_service.mark_step(job.id, "syncing_qdrant", 100)

                logger.info(
                    "step_completed",
                    job_id=job.id,
                    step="syncing_qdrant",
                    audio_features=feature_vector is not None,
                    lyrics_embeddings=lyric_vector is not None,
                )

            # ------------------------------------------------------------------
            # Finalize
            # ------------------------------------------------------------------
            clip_path = None  # Video generation skipped — frontend renders lyrics

            steps_completed = ["separating"]
            if transcription is not None:
                steps_completed.append("transcribing")
            if feature_vector is not None:
                steps_completed.append("extracting_features")
            if lyric_vector is not None:
                steps_completed.append("embedding_lyrics")
            if qdrant_synced:
                steps_completed.append("syncing_qdrant")

            # Mark the track as ready so it appears in search, popular, etc.
            update = TrackUpdate(status="ready")
            if qdrant_synced:
                update.qdrant_synced = 1
            await self.repo.update_track(job.track_id, update)

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
