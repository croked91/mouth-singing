"""Audio Worker entry point.

Starts the GPU pipeline and consumes jobs from RabbitMQ.
API mode (MVSEP + OpenAI Whisper) has been removed — only GPU mode remains.
"""

from __future__ import annotations

import asyncio
import signal

import asyncpg
import structlog

from karaoke_shared.messaging.rabbitmq import RabbitMQClient
from karaoke_shared.services.job_service import JobService
from karaoke_shared.services.progress_publisher import ProgressPublisher
from karaoke_shared.storage import S3Storage

from worker.app.config import settings
from worker.app.consumer import JobConsumer
from worker.common.base_pipeline import BasePipeline

logger = structlog.get_logger(__name__)


async def _open_pg(dsn: str) -> asyncpg.Pool:
    """Create an asyncpg connection pool to PostgreSQL."""
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    return pool


def _build_gpu_pipeline(
    job_service: JobService,
    repo,
    storage: S3Storage,
    rmq: RabbitMQClient,
) -> BasePipeline:
    """Construct and return a GpuPipeline with all its components."""
    from worker.gpu.gpu_pipeline import GpuPipeline
    from worker.gpu.uvr_separator import UVRSeparator
    from worker.gpu.whisper_transcriber import WhisperTranscriber
    from worker.common.vad_processor import VADProcessor
    from worker.gpu.torch_ctc_aligner import TorchCTCAligner
    from worker.common.lyrics_agent import LyricsAgent
    from worker.common.lyrics import LyricsProviderChain
    from worker.common.lyrics.providers.lrclib import LRCLibProvider
    from worker.common.lyrics.providers.lyricsovh import LyricsOvhProvider
    from worker.common.lyrics.verifier import LyricsVerifier

    uvr = UVRSeparator(
        model_cache_dir=settings.model_cache_dir,
        media_root=settings.media_root,
        model_name=settings.uvr_model_name,
        chunk_batch_size=settings.uvr_chunk_batch_size,
        use_autocast=settings.uvr_use_autocast,
        overlap=settings.uvr_overlap,
    )
    whisper = WhisperTranscriber(
        model_size=settings.whisper_model_size,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        model_cache_dir=settings.model_cache_dir,
    )

    vad = VADProcessor(top_db=settings.vad_top_db)

    # -- Lyrics provider chain ------------------------------------------------
    provider_timeout = settings.lyrics_provider_timeout

    # Genius searches by lyrics text; LRCLib/Lyrics.ovh search by artist+title
    text_providers = []
    if settings.genius_token:
        from worker.common.lyrics.providers.genius import GeniusProvider
        text_providers.append(
            GeniusProvider(token=settings.genius_token, timeout=provider_timeout),
        )

    metadata_providers = [
        LRCLibProvider(timeout=provider_timeout),
        LyricsOvhProvider(timeout=provider_timeout),
    ]

    verifier = (
        LyricsVerifier(
            deepseek_api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
        )
        if settings.deepseek_api_key
        else None
    )

    fallback_agent = None
    if (
        settings.deepseek_api_key
        and settings.yandex_search_api_key
        and settings.yandex_search_folder_id
    ):
        fallback_agent = LyricsAgent(
            deepseek_api_key=settings.deepseek_api_key,
            yandex_search_api_key=settings.yandex_search_api_key,
            yandex_search_folder_id=settings.yandex_search_folder_id,
            model=settings.deepseek_model,
            max_iterations=settings.lyrics_agent_max_iterations,
            timeout=settings.lyrics_agent_timeout,
        )

    lyrics_searcher = LyricsProviderChain(
        text_providers=text_providers,
        metadata_providers=metadata_providers,
        verifier=verifier,
        fallback_agent=fallback_agent,
        search_fragments=settings.lyrics_search_fragments,
    )
    logger.info(
        "lyrics_chain_enabled",
        text_providers=[p.name for p in text_providers],
        metadata_providers=[p.name for p in metadata_providers],
        has_verifier=verifier is not None,
        has_fallback=fallback_agent is not None,
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
        storage=storage,
        rmq=rmq,
        settings=settings,
    )


async def main() -> None:
    """Worker entry point."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

    logger.info("worker_starting", worker_id=settings.worker_id)

    pool = await _open_pg(settings.pg_dsn)

    # RabbitMQ connection.
    rmq = RabbitMQClient(settings.rabbitmq_url)
    await rmq.connect()
    await rmq.declare_topology()

    # S3 storage.
    storage = S3Storage(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )

    try:
        from karaoke_shared.repositories.pg_repository import PgRepository

        repo = PgRepository(pool)

        # ProgressPublisher sends step updates via RabbitMQ.
        publisher = ProgressPublisher(rmq)
        job_service = JobService(repo, publisher=publisher)

        # Reset stale jobs from any previous crash.
        reset_count = await repo.reset_stale_running_jobs(settings.worker_id)
        if reset_count:
            logger.info("stale_jobs_reset", count=reset_count)

        pipeline = _build_gpu_pipeline(job_service, repo, storage, rmq)

        consumer = JobConsumer(
            rmq=rmq,
            pipeline=pipeline,
            repo=repo,
            job_service=job_service,
            worker_id=settings.worker_id,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, consumer.stop)

        await consumer.run()

    finally:
        if 'pipeline' in dir() and hasattr(pipeline, "cleanup"):
            pipeline.cleanup()
        await rmq.close()
        await pool.close()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
