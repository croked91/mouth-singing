"""Worker configuration loaded from environment variables.

Only GPU mode is supported. API mode (MVSEP + OpenAI Whisper) has been removed.
"""

from __future__ import annotations

import os
import socket

from pydantic_settings import BaseSettings


class WorkerSettings(BaseSettings):
    """Runtime configuration for the karaoke audio worker (GPU mode only)."""

    # ------------------------------------------------------------------
    # Common: infrastructure
    # ------------------------------------------------------------------

    pg_dsn: str = "postgresql://karaoke:karaoke@postgres:5432/karaoke"
    media_root: str = "/data/media"

    # S3-compatible storage
    s3_bucket: str = "karaoke"
    s3_endpoint_url: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"

    # RabbitMQ
    rabbitmq_url: str = "amqp://karaoke:karaoke@rabbitmq:5672/"
    model_cache_dir: str = "/data/models"
    worker_id: str = f"{socket.gethostname()}-{os.getpid()}"
    poll_interval_sec: float = 2.0
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # Common: lyrics agent (DeepSeek + Yandex Search)
    # ------------------------------------------------------------------

    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    yandex_search_api_key: str = ""
    yandex_search_folder_id: str = ""
    lyrics_agent_max_iterations: int = 15
    lyrics_agent_timeout: float = 15.0

    # ------------------------------------------------------------------
    # Common: CTC aligner
    # ------------------------------------------------------------------

    ctc_min_frames_for_char: int = 10
    ctc_device: str = "cpu"
    """ONNX execution provider for CTC alignment: 'cuda' or 'cpu'.
    CPU is the only viable option — wav2vec2 ONNX graph has 24 ops
    unsupported by CUDA EP, causing constant CPU<->GPU memcpy that
    makes GPU slower than pure CPU."""
    ctc_batch_size: int = 16
    """Batch size for generate_emissions (CPU has plenty of RAM)."""

    # ------------------------------------------------------------------
    # Common: VAD
    # ------------------------------------------------------------------

    vad_top_db: int = 35

    # ------------------------------------------------------------------
    # GPU mode: UVR local separator
    # ------------------------------------------------------------------

    uvr_model_name: str = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
    uvr_torch_device: str = "cuda"

    # ------------------------------------------------------------------
    # GPU mode: faster-whisper local ASR
    # ------------------------------------------------------------------

    whisper_model_size: str = "tiny"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"

    model_config = {"env_prefix": ""}


settings = WorkerSettings()
