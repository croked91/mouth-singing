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

    # Job sweeper — periodic recovery of orphan pending jobs whose RMQ
    # message was lost (broker volume reset, queue recreated, INSERT vs
    # publish race, admin purge). See backend/app/services/job_sweeper.py.
    sweeper_interval_sec: int = 300         # how often to scan (5 min)
    sweeper_pending_ttl_sec: int = 600      # republish if pending > 10 min
    sweeper_hard_fail_ttl_sec: int = 86400  # give up if pending > 24 h

    model_config = {"env_prefix": ""}


# Module-level singleton used by other modules.
settings = Settings()

if settings.admin_secret == "changeme":
    structlog.get_logger(__name__).warning(
        "admin_secret_is_default",
        hint="Set ADMIN_SECRET environment variable for production",
    )
