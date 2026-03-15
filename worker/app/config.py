"""Worker configuration loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class WorkerSettings(BaseSettings):
    """Runtime configuration for the v3-rc1 audio worker."""

    database_url: str = "/data/sqlite/karaoke.db"
    media_root: str = "/data/media"
    model_cache_dir: str = "/data/models"
    worker_id: str = "worker-1"
    poll_interval_sec: float = 2.0
    log_level: str = "INFO"

    # Audio feature normalization stats (z-score).
    normalization_stats_path: str = ""

    # QDrant vector database.
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333

    # UVR (changed: GPU + BS-Roformer model)
    uvr_model_name: str = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
    uvr_torch_device: str = "cuda"

    # Whisper ASR (NEW)
    whisper_model_size: str = "tiny"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"

    # VAD (NEW)
    vad_top_db: int = 35

    # Lyrics search: LLM identification + Genius fetch (NEW)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_timeout: float = 30.0
    openai_max_retries: int = 2
    openai_base_url: str = "https://api.openai.com"
    genius_token: str = ""

    # CTC aligner (NEW)
    ctc_min_frames_for_char: int = 10

    model_config = {"env_prefix": ""}


settings = WorkerSettings()
