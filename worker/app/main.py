"""Audio Worker entry point.

Reads WORKER_MODE from the environment (default: 'gpu') and starts
either GpuPipeline or ApiPipeline, then polls for pending jobs.

Supported modes
---------------
gpu  — local UVR (BS-Roformer) + faster-whisper + CTC + sentence-transformers
api  — MVSEP API + OpenAI Whisper API + CTC + optional OpenAI embeddings
"""

from __future__ import annotations

import asyncio
import pathlib
import signal

import aiosqlite
import structlog

from karaoke_shared.services.job_service import JobService

from worker.app.config import settings
from worker.common.base_pipeline import BasePipeline

logger = structlog.get_logger(__name__)


async def _open_db(db_path: str) -> aiosqlite.Connection:
    """Open the SQLite database in WAL mode."""
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
        pipeline: BasePipeline,
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
        logger.info(
            "poller_started",
            worker_id=self.worker_id,
            interval=self.poll_interval,
        )

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


def _build_gpu_pipeline(
    job_service: JobService,
    repo,
    qdrant_repo,
    feature_extractor,
    lyric_embedder,
) -> BasePipeline:
    """Construct and return a GpuPipeline with all its components."""
    from worker.gpu.gpu_pipeline import GpuPipeline
    from worker.gpu.uvr_separator import UVRSeparator
    from worker.gpu.whisper_transcriber import WhisperTranscriber
    from worker.common.vad_processor import VADProcessor
    from worker.gpu.torch_ctc_aligner import TorchCTCAligner
    from worker.common.lyrics_agent import LyricsAgent

    uvr = UVRSeparator(
        model_cache_dir=settings.model_cache_dir,
        media_root=settings.media_root,
        model_name=settings.uvr_model_name,
    )

    whisper = WhisperTranscriber(
        model_size=settings.whisper_model_size,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        model_cache_dir=settings.model_cache_dir,
    )

    vad = VADProcessor(top_db=settings.vad_top_db)

    lyrics_searcher: LyricsAgent | None = None
    if (
        settings.deepseek_api_key
        and settings.yandex_search_api_key
        and settings.yandex_search_folder_id
    ):
        lyrics_searcher = LyricsAgent(
            deepseek_api_key=settings.deepseek_api_key,
            yandex_search_api_key=settings.yandex_search_api_key,
            yandex_search_folder_id=settings.yandex_search_folder_id,
            model=settings.deepseek_model,
            max_iterations=settings.lyrics_agent_max_iterations,
            timeout=settings.lyrics_agent_timeout,
        )
        logger.info("lyrics_agent_enabled", model=settings.deepseek_model)
    else:
        logger.error(
            "lyrics_agent_keys_missing",
            deepseek=bool(settings.deepseek_api_key),
            yandex_key=bool(settings.yandex_search_api_key),
            yandex_folder=bool(settings.yandex_search_folder_id),
        )

    ctc_aligner = TorchCTCAligner(
        device="cuda",
        model_cache_dir=settings.model_cache_dir,
    )

    return GpuPipeline(
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


def _build_api_pipeline(
    job_service: JobService,
    repo,
    qdrant_repo,
    feature_extractor,
    lyric_embedder,
) -> BasePipeline:
    """Construct and return an ApiPipeline with all its components."""
    from worker.api.api_pipeline import ApiPipeline
    from worker.api.mvsep_client import MVSEPClient
    from worker.api.whisper_client import WhisperAPIClient
    from worker.common.vad_processor import VADProcessor
    from worker.common.ctc_aligner import CTCAligner
    from worker.common.lyrics_agent import LyricsAgent
    from karaoke_shared.utils.syllabifier import Syllabifier

    mvsep = MVSEPClient(
        api_key=settings.mvsep_api_key,
        api_url=settings.mvsep_api_url,
        sep_type=settings.mvsep_sep_type,
        output_format=settings.mvsep_output_format,
        poll_interval_sec=settings.mvsep_poll_interval_sec,
        timeout_sec=settings.mvsep_timeout_sec,
        media_root=settings.media_root,
    )

    whisper = WhisperAPIClient(
        api_key=settings.openai_api_key,
        model=settings.whisper_api_model,
        timeout=settings.whisper_api_timeout,
    )

    vad = VADProcessor(top_db=settings.vad_top_db)

    lyrics_searcher: LyricsAgent | None = None
    if (
        settings.deepseek_api_key
        and settings.yandex_search_api_key
        and settings.yandex_search_folder_id
    ):
        lyrics_searcher = LyricsAgent(
            deepseek_api_key=settings.deepseek_api_key,
            yandex_search_api_key=settings.yandex_search_api_key,
            yandex_search_folder_id=settings.yandex_search_folder_id,
            model=settings.deepseek_model,
            max_iterations=settings.lyrics_agent_max_iterations,
            timeout=settings.lyrics_agent_timeout,
        )
        logger.info("lyrics_agent_enabled", model=settings.deepseek_model)
    else:
        logger.error(
            "lyrics_agent_keys_missing",
            deepseek=bool(settings.deepseek_api_key),
            yandex_key=bool(settings.yandex_search_api_key),
            yandex_folder=bool(settings.yandex_search_folder_id),
        )

    ctc_aligner = CTCAligner(
        syllabifier=Syllabifier(),
        model_cache_dir=settings.model_cache_dir,
        min_frames_for_char=settings.ctc_min_frames_for_char,
        device=settings.ctc_device,
    )

    return ApiPipeline(
        job_service=job_service,
        repo=repo,
        mvsep=mvsep,
        whisper=whisper,
        vad=vad,
        lyrics_searcher=lyrics_searcher,
        ctc_aligner=ctc_aligner,
        feature_extractor=feature_extractor,
        lyric_embedder=lyric_embedder,
        qdrant_repo=qdrant_repo,
        settings=settings,
    )


def _load_ml_components(
    repo,
) -> tuple[object | None, object | None, object | None]:
    """Load feature extractor, lyric embedder, and QDrant repo.

    Returns:
        (feature_extractor, lyric_embedder, qdrant_repo) — any may be None
        if the component fails to load.
    """
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

    if settings.worker_mode == "api" and settings.lyric_embedder_backend == "openai":
        try:
            from worker.api.openai_embedder import OpenAIEmbedder

            lyric_embedder = OpenAIEmbedder(
                api_key=settings.openai_api_key,
                model=settings.openai_embedding_model,
                dimensions=settings.openai_embedding_dimensions,
            )
            logger.info("lyric_embedder_loaded", backend="openai")
        except Exception:
            logger.warning("lyric_embedder_unavailable", backend="openai")
    else:
        try:
            from karaoke_shared.ml.lyric_embedder import LyricEmbedder

            lyric_embedder = LyricEmbedder(cache_dir=settings.model_cache_dir)
            logger.info("lyric_embedder_loaded", backend="local")
        except Exception:
            logger.warning("lyric_embedder_unavailable", backend="local")

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

    return feature_extractor, lyric_embedder, qdrant_repo


async def main() -> None:
    """Worker entry point."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

    mode = settings.worker_mode
    logger.info("worker_starting", worker_id=settings.worker_id, mode=mode)

    if mode not in ("gpu", "api"):
        raise ValueError(
            f"Unknown WORKER_MODE={mode!r}. Must be 'gpu' or 'api'."
        )

    db = await _open_db(settings.database_url)

    try:
        from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

        repo = SQLiteRepository(db)
        job_service = JobService(repo)

        # Reset stale jobs from any previous crash.
        reset_count = await repo.reset_stale_running_jobs(settings.worker_id)
        if reset_count:
            logger.info("stale_jobs_reset", count=reset_count)

        # ML components are mode-independent (feature extractor, embedder, QDrant).
        feature_extractor, lyric_embedder, qdrant_repo = _load_ml_components(repo)

        if mode == "gpu":
            pipeline = _build_gpu_pipeline(
                job_service, repo, qdrant_repo, feature_extractor, lyric_embedder,
            )
        else:
            pipeline = _build_api_pipeline(
                job_service, repo, qdrant_repo, feature_extractor, lyric_embedder,
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
        if hasattr(pipeline, "cleanup"):
            pipeline.cleanup()
        await db.close()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
