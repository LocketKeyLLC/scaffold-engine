"""Scaffold Engine configuration — loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All config sourced from env vars set in docker-compose.yml."""

    # Database
    database_url: str = "postgresql+asyncpg://scaffold:scaffold_dev_pw@scaffold-postgres:5432/scaffold_engine"

    # External services
    ollama_base_url: str = "http://172.18.0.1:11434"
    milvus_uri: str = "http://milvus-standalone:19530"
    searxng_url: str = "http://searxng:8080"
    # Redis cache
    redis_url: str = "redis://scaffold-redis:6379/0"
    # Embedding config
    embedding_dim: int = 512
    model_embedder_id: str = "qwen3-embedding-8b-mrl512"

    # Model assignments
    model_router: str = "qwen3:4b"
    model_embedder_pipeline: str = "qwen3-embedding:8b"
    model_reranker: str = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
    model_coder: str = "qwen2.5-coder:7b"
    model_general: str = "qwen3-vl:235b-instruct-cloud"
    model_verifier: str = "qwen2.5:7b"
    model_cloud_heavy: str = "qwen3-vl:235b-instruct-cloud"
    model_cloud_alt: str = "qwen3.5:397b-cloud"
    model_fallback: str = "qwen3.5:latest"

    # Timeouts (seconds)
    cloud_timeout: int = 3600
    local_timeout: int = 1800
    max_retries: int = 3

    # Logging
    log_level: str = "info"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
