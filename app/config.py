"""Scaffold Engine configuration — loaded from environment variables."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All config sourced from env vars set in docker-compose.yml."""

    # Database
    database_url: str = "postgresql+asyncpg://scaffold:scaffold_dev_pw@scaffold-postgres:5432/scaffold_engine"

    @property
    def sync_database_url(self) -> str:
        """Sync DSN for APScheduler's SQLAlchemyJobStore (no asyncpg)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")

    scheduler_enabled: bool = True
    scheduler_timezone: str = "UTC"
    scheduler_jobstore_url: str = ""  # empty = derive from sync_database_url

    # External services
    ollama_base_url: str = "http://172.18.0.1:11434"
    milvus_uri: str = "http://milvus-standalone:19530"
    searxng_url: str = "http://searxng:8080"
    # Redis cache
    redis_url: str = "redis://scaffold-redis:6379/0"
    # Embedding config
    embedding_dim: int = 512
    model_embedder_id: str = "qwen3-embedding-8b-mrl512"
    semantic_dedup_threshold: float = 0.95

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

    # Research agent
    research_max_iterations: int = 3
    research_max_queries: int = 8
    research_max_urls_per_iteration: int = 20
    research_searxng_delay: float = 1.5
    research_chunk_size: int = 1500
    research_timeout: int = 3600
    github_token: str = ""
    github_max_files: int = 50
    github_timeout: int = 30
    github_api_base: str = "https://api.github.com"
    openapi_max_endpoints: int = 200
    openapi_timeout: int = 30

    # Logging
    log_level: str = "info"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


def get_model(role: str, overrides: dict | None = None) -> str:
    """Return model tag: override > env var > default."""
    if overrides and overrides.get(role):
        return overrides[role]
    return getattr(settings, role)
