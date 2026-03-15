"""Audio Worker entry point (v3-rc1).

Runs an asyncio event loop that polls for pending jobs and processes them
through the new AudioPipeline with CTC alignment, Whisper ASR, and
LLM-based lyrics search.
"""

from __future__ import annotations

import asyncio
import pathlib
import signal

import aiosqlite
import structlog

from karaoke_shared.services.job_service import JobService

from app.config import settings
from app.pipeline.audio_pipeline import AudioPipeline
from app.pipeline.uvr_separator import UVRSeparator

logger = structlog.get_logger(__name__)


async def _open_db(db_path: str) -> aiosqlite.Connection:
    """Open an existing SQLite database in WAL mode."""
    path = pathlib.Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


class JobPoller:
    """Polls for pending jobs and dispatches them to the pipeline."""

    def __init__(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        worker_id: str,
        poll_interval: float,
    ) -> None:
        self.pipeline = pipeline
        self.job_service = job_service
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self._running = True

    def stop(self) -> None:
        logger.info("shutdown_requested", worker_id=self.worker_id)
        self._running = False

    async def run(self) -> None:
        logger.info("poller_started", worker_id=self.worker_id, interval=self.poll_interval)

        while self._running:
            try:
                job = await self.job_service.poll_and_lock(self.worker_id)

                if job is not None:
                    logger.info("job_acquired", job_id=job.id, track_id=job.track_id)
                    await self.pipeline.process(job)
                    logger.info("job_processed", job_id=job.id)
                else:
                    await asyncio.sleep(self.poll_interval)

            except Exception as exc:
                logger.error("poller_error", error=str(exc))
                await asyncio.sleep(self.poll_interval)


async def main() -> None:
    """Worker entry point."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

    logger.info("worker_starting", worker_id=settings.worker_id, version="v3-rc1")

    db = await _open_db(settings.database_url)

    try:
        from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

        repo = SQLiteRepository(db)
        job_service = JobService(repo)

        # --- UVR separator (GPU) ---
        uvr = UVRSeparator(
            model_cache_dir=settings.model_cache_dir,
            media_root=settings.media_root,
            model_name=settings.uvr_model_name,
        )

        # --- Whisper ASR ---
        from app.pipeline.whisper_transcriber import WhisperTranscriber

        whisper = WhisperTranscriber(
            model_size=settings.whisper_model_size,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            model_cache_dir=settings.model_cache_dir,
        )

        # --- VAD ---
        from app.pipeline.vad_processor import VADProcessor

        vad = VADProcessor(top_db=settings.vad_top_db)

        # --- Lyrics searcher ---
        from app.pipeline.lyrics_searcher import LyricsSearcher

        lyrics_searcher: LyricsSearcher | None = None
        if settings.openai_api_key and settings.genius_token:
            lyrics_searcher = LyricsSearcher(
                openai_api_key=settings.openai_api_key,
                genius_token=settings.genius_token,
                model=settings.openai_model,
                timeout=settings.openai_timeout,
                max_retries=settings.openai_max_retries,
                openai_base_url=settings.openai_base_url,
            )
            logger.info("lyrics_searcher_enabled", model=settings.openai_model)
        else:
            logger.error("lyrics_searcher_keys_missing",
                         openai=bool(settings.openai_api_key),
                         genius=bool(settings.genius_token))

        # --- CTC aligner ---
        from karaoke_shared.utils.syllabifier import Syllabifier
        from app.pipeline.ctc_aligner import CTCAligner

        ctc_aligner = CTCAligner(
            syllabifier=Syllabifier(),
            model_cache_dir=settings.model_cache_dir,
            min_frames_for_char=settings.ctc_min_frames_for_char,
        )

        # --- ML components ---
        feature_extractor = None
        lyric_embedder = None
        qdrant_repo = None

        try:
            from karaoke_shared.ml.feature_extractor import FeatureExtractor

            fe_kwargs: dict = {}
            if settings.normalization_stats_path:
                fe_kwargs["normalization_stats_path"] = settings.normalization_stats_path
            feature_extractor = FeatureExtractor(**fe_kwargs)
            logger.info(
                "feature_extractor_loaded",
                normalization_stats=settings.normalization_stats_path or "none",
            )
        except Exception:
            logger.warning("feature_extractor_unavailable")

        try:
            from karaoke_shared.ml.lyric_embedder import LyricEmbedder

            lyric_embedder = LyricEmbedder(cache_dir=settings.model_cache_dir)
            logger.info("lyric_embedder_loaded")
        except Exception:
            logger.warning("lyric_embedder_unavailable")

        if feature_extractor is not None or lyric_embedder is not None:
            try:
                from qdrant_client import QdrantClient
                from karaoke_shared.repositories.qdrant_repository import QDrantRepository

                qdrant_client = QdrantClient(
                    host=settings.qdrant_host,
                    port=settings.qdrant_port,
                )
                qdrant_repo = QDrantRepository(qdrant_client)
                logger.info("qdrant_connected", host=settings.qdrant_host)
            except Exception:
                logger.warning("qdrant_unavailable")

        # Reset stale jobs from previous crash
        reset_count = await repo.reset_stale_running_jobs(settings.worker_id)
        if reset_count:
            logger.info("stale_jobs_reset", count=reset_count)

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=uvr,
            repo=repo,
            whisper=whisper,
            vad_processor=vad,
            lyrics_searcher=lyrics_searcher,
            ctc_aligner=ctc_aligner,
            feature_extractor=feature_extractor,
            lyric_embedder=lyric_embedder,
            qdrant_repo=qdrant_repo,
            settings=settings,
        )

        poller = JobPoller(
            pipeline=pipeline,
            job_service=job_service,
            worker_id=settings.worker_id,
            poll_interval=settings.poll_interval_sec,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, poller.stop)

        await poller.run()

    finally:
        await db.close()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
