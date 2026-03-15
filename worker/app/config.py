"""Unified worker configuration loaded from environment variables.

Fields are grouped by concern.  Each pipeline mode reads only the fields
it needs, but keeping them in one place means a single .env file covers
both GPU and API deployments.
"""

from __future__ import annotations

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
    worker_id: str = "worker-1"
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
    # Common: lyrics search (shared by both modes)
    # ------------------------------------------------------------------

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_timeout: float = 30.0
    openai_max_retries: int = 2
    openai_base_url: str = "https://api.openai.com"
    genius_token: str = ""

    # ------------------------------------------------------------------
    # Common: CTC aligner
    # ------------------------------------------------------------------

    ctc_min_frames_for_char: int = 10

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
