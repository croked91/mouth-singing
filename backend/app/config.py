"""Application configuration loaded from environment variables.

All settings have sensible defaults for local development. In production
(Docker Compose), the real values are injected via the environment block
in docker-compose.yml.
"""

import structlog
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Top-level settings object.

    Reads values from environment variables (case-insensitive).
    No env_prefix is used so that the variable names in docker-compose.yml
    map directly (e.g. PG_DSN -> pg_dsn).
    """

    pg_dsn: str = "postgresql://karaoke:karaoke@localhost:5432/karaoke"
    admin_secret: str = "changeme"
    log_level: str = "INFO"

    # S3-compatible storage (MinIO / AWS S3 / Yandex OS)
    s3_bucket: str = "karaoke"
    s3_endpoint_url: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    s3_presigned_url_base: str = ""

    # RabbitMQ
    rabbitmq_url: str = "amqp://karaoke:karaoke@rabbitmq:5672/"

    # Rec-service (recommendation microservice)
    rec_service_url: str = "http://rec-service:8001"
    rec_service_timeout: float = 5.0

    # DeepSeek LLM for mood/theme query expansion (optional)
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"

    model_config = {"env_prefix": ""}


# Module-level singleton used by other modules.
settings = Settings()

if settings.admin_secret == "changeme":
    structlog.get_logger(__name__).warning(
        "admin_secret_is_default",
        hint="Set ADMIN_SECRET environment variable for production",
    )
