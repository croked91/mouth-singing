"""Worker configuration loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class WorkerSettings(BaseSettings):
    """Runtime configuration for the v3-rc2 audio worker (VPS + API, no GPU)."""

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

    # VAD
    vad_top_db: int = 35

    # MVSEP API (cloud stem separation, replaces local UVR).
    mvsep_api_key: str = ""
    mvsep_api_url: str = "https://mvsep.com/api"
    mvsep_sep_type: int = 49
    mvsep_output_format: str = "mp3"
    mvsep_poll_interval_sec: float = 10.0
    mvsep_timeout_sec: float = 600.0
    mvsep_max_retries: int = 3

    # OpenAI Whisper API (cloud ASR, replaces local Whisper).
    whisper_model: str = "whisper-1"
    whisper_timeout_sec: float = 120.0
    whisper_max_retries: int = 2
    whisper_language_hint: str = ""  # empty string = auto-detect

    # Lyrics search: LLM identification + Genius fetch.
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_timeout: float = 30.0
    openai_max_retries: int = 2
    openai_base_url: str = "https://api.openai.com"
    genius_token: str = ""

    # Lyric embeddings backend.
    lyric_embedder_backend: str = "local"  # "local" | "openai"
    openai_embed_model: str = "text-embedding-3-small"
    openai_embed_dimensions: int = 384
    openai_embed_timeout_sec: float = 30.0

    # CTC aligner.
    ctc_min_frames_for_char: int = 10

    # Cost tracking.
    cost_tracking_enabled: bool = True

    model_config = {"env_prefix": ""}


settings = WorkerSettings()
