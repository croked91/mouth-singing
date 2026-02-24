"""Worker configuration loaded from environment variables.

All settings have sensible defaults that match the Docker Compose volume paths.
Override via environment variables — no prefix is used so that variable names
match exactly (e.g. DATABASE_URL, MEDIA_ROOT).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class WorkerSettings(BaseSettings):
    """Runtime configuration for the audio worker process."""

    database_url: str = "/data/sqlite/karaoke.db"
    media_root: str = "/data/media"
    model_cache_dir: str = "/data/models"
    worker_id: str = "worker-1"
    poll_interval_sec: float = 2.0
    log_level: str = "INFO"

    # QDrant vector database settings (for feature/embedding sync).
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333

    # Soniox Speech-to-Text API settings.
    sonoix_api_key: str = ""
    sonoix_api_url: str = "https://api.soniox.com"
    sonoix_timeout: float = 120.0

    model_config = {"env_prefix": ""}


settings = WorkerSettings()
