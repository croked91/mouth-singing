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

    uvr = UVRSeparator(
        model_cache_dir=settings.model_cache_dir,
        media_root=settings.media_root,
        model_name=settings.uvr_model_name,
        batch_size=settings.uvr_batch_size,
        use_autocast=settings.uvr_use_autocast,
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
