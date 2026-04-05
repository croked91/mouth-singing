from pydantic_settings import BaseSettings


class RecServiceSettings(BaseSettings):
    pg_dsn: str = "postgresql://karaoke:karaoke@postgres:5432/karaoke"
    rabbitmq_url: str = "amqp://karaoke:karaoke@rabbitmq:5672/"
    s3_bucket: str = "karaoke"
    s3_endpoint_url: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333
    normalization_stats_path: str = ""
    rec_cluster_centroids_path: str = ""
    log_level: str = "INFO"
    model_config = {"env_prefix": ""}


settings = RecServiceSettings()
