"""Unified worker configuration loaded from environment variables.

Fields are grouped by concern.  Each pipeline mode reads only the fields
it needs, but keeping them in one place means a single .env file covers
both GPU and API deployments.
"""

from __future__ import annotations

import os
import socket

from pydantic_settings import BaseSettings


class WorkerSettings(BaseSettings):
    """Runtime configuration for the karaoke audio worker.

    Set WORKER_MODE=gpu (default) or WORKER_MODE=api to select the
    processing pipeline at startup.
    """

    # ------------------------------------------------------------------
    # Pipeline selection
    # ------------------------------------------------------------------

    worker_mode: str = "gpu"
    """Which pipeline to use: 'gpu' (local UVR + faster-whisper) or
    'api' (MVSEP + OpenAI Whisper API)."""

    # ------------------------------------------------------------------
    # Common: infrastructure
    # ------------------------------------------------------------------

    database_url: str = "/data/sqlite/karaoke.db"
    media_root: str = "/data/media"
    model_cache_dir: str = "/data/models"
    worker_id: str = f"{socket.gethostname()}-{os.getpid()}"
    poll_interval_sec: float = 2.0
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # Common: QDrant
    # ------------------------------------------------------------------

    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333

    # ------------------------------------------------------------------
    # Common: audio feature normalization
    # ------------------------------------------------------------------

    normalization_stats_path: str = ""
    """Path to feature_normalization_stats.json.  Empty = skip z-score."""

    # ------------------------------------------------------------------
    # Common: OpenAI key (used by Whisper API + optional embedder)
    # ------------------------------------------------------------------

    openai_api_key: str = ""

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
    CPU is recommended — subprocess isolation prevents heap corruption
    from crashing the main worker, and avoids VRAM contention."""
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

    # ------------------------------------------------------------------
    # API mode: MVSEP stem separation
    # ------------------------------------------------------------------

    mvsep_api_key: str = ""
    mvsep_api_url: str = "https://mvsep.com/api"
    mvsep_sep_type: int = 49
    """MVSEP model type ID.  49 = BS-Roformer."""
    mvsep_output_format: str = "mp3"
    mvsep_poll_interval_sec: float = 10.0
    mvsep_timeout_sec: float = 600.0

    # ------------------------------------------------------------------
    # API mode: OpenAI Whisper API ASR
    # ------------------------------------------------------------------

    whisper_api_model: str = "whisper-1"
    whisper_api_timeout: float = 120.0

    # ------------------------------------------------------------------
    # API mode: lyric embedder backend
    # ------------------------------------------------------------------

    lyric_embedder_backend: str = "local"
    """'local' = sentence-transformers, 'openai' = text-embedding-3-small."""

    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 384

    model_config = {"env_prefix": ""}


settings = WorkerSettings()
