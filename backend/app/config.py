"""Application configuration loaded from environment variables.

All settings have sensible defaults for local development. In production
(Docker Compose), the real values are injected via the environment block
in docker-compose.yml.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Top-level settings object.

    Reads values from environment variables (case-insensitive).
    No env_prefix is used so that the variable names in docker-compose.yml
    map directly (e.g. DATABASE_URL -> database_url).
    """

    database_url: str = "/data/sqlite/karaoke.db"
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    media_root: str = "/data/media"
    admin_secret: str = "changeme"
    log_level: str = "INFO"

    model_config = {"env_prefix": ""}


# Module-level singleton used by other modules.
settings = Settings()
