"""Scaffold Engine configuration — loaded from environment variables."""
from pydantic_settings import BaseSettings


# ---------------------------------------------------------------------------
# DAG validation enums (#101 — moved from dag_generator.py module top)
# These are plain module constants, not Settings fields, because they are
# code-level invariants and not env-overridable.
# ---------------------------------------------------------------------------
VALID_TASK_TYPES = frozenset({"research", "decision", "action", "validation", "output"})
VALID_STRATEGIES = frozenset({"sequential", "parallel", "hybrid", "conditional"})
VALID_TOOLS = frozenset({"LLM", "CodeGen", "SearXNG", "Milvus"})
VALID_DOMAINS = frozenset({"prompt", "rag", "eng", "llm", "spec"})



# ---------------------------------------------------------------------------
# TTL policy by source_type (seconds) — code-level invariant, not env-overridable
# ---------------------------------------------------------------------------
TTL_POLICY = {
    "real_time": 7 * 86400,        # 7 days
    "news": 30 * 86400,            # 30 days
    "community": 90 * 86400,       # 90 days
    "tech_docs": 180 * 86400,      # 6 months
    "curated": 365 * 86400,        # 1 year
    "official_docs": 365 * 86400,  # 1 year
    "ai_generated": 180 * 86400,   # 6 months
}
DEFAULT_TTL_SECONDS = 180 * 86400  # fallback for unknown source_types

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
    scheduler_jobstore_url: str = ""
    scheduler_job_timeout: int = 3600
    """Max seconds a single scheduled research job may run before being cancelled."""
    scheduler_misfire_grace_time: int = 300
    """Seconds APScheduler will still fire a missed job after its scheduled time."""
    scheduler_shutdown_timeout: int = 30
    """Seconds to wait for in-flight jobs during graceful shutdown before forcing exit."""

    # External services
    ollama_base_url: str = "http://172.18.0.1:11434"
    milvus_uri: str = "http://milvus-standalone:19530"
    milvus_num_partitions: int = 64
    embedding_cache_memory_size: int = 10_000
    # Reranker prompt template (default: Qwen3-Reranker format)
    reranker_prompt_system: str = (
        "<|im_start|>system\n"
        "Judge whether the Document meets the requirements based on the Query "
        "and the Instruct provided. Note that the answer can only be \"yes\" "
        "or \"no\".<|im_end|>\n<|im_start|>user\n"
    )
    reranker_prompt_suffix: str = (
        "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )
    reranker_default_instruction: str = (
        "Given a web search query, retrieve relevant passages that answer the query"
    )
    searxng_url: str = "http://searxng:8080"
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
    # Ideation pipeline caps (Phase 2 research_and_compile)
    ideation_max_queries: int = 5
    ideation_max_distill_results: int = 15
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

    # GT pipeline — GitHub push target
    gt_github_owner: str = "LocketKeyLLC"
    gt_github_repo: str = "smokieRAGs"
    gt_github_branch: str = "main"

    # GT browser — stats scan cap (Milvus max per query is 16384)
    gt_stats_scan_limit: int = 16384

    # Research agent — fetch caps & concurrency (new: phase B)
    research_max_url_bytes: int = 5 * 1024 * 1024
    """Per-URL byte cap for bounded fetch in URL-mode and trafilatura batch."""
    research_max_pdf_bytes: int = 20 * 1024 * 1024
    """Per-PDF byte cap for uploads to /research/pdf."""
    research_fetch_concurrency: int = 5
    """Semaphore size for concurrent trafilatura page fetches."""
    research_fetch_timeout: int = 15
    """httpx timeout (s) for trafilatura batch fetches."""
    research_url_fetch_timeout: int = 30
    """httpx timeout (s) for URL-mode bounded fetch + robots check."""
    research_heartbeat_interval: int = 8
    """Seconds between SSE heartbeat emissions during long LLM calls."""
    research_max_entry_chars: int = 8000
    """Max chars per ingested entry content (GitHub/OpenAPI truncation)."""

    # Research agent — topic → Milvus partition domain (new: phase B)
    topic_to_domain: dict[int, str] = {
        1: "llm",
        2: "rag",
        3: "eng",
        4: "eng",
        5: "eng",
        6: "eng",
    }
    """Maps _detect_topic_id() output → Milvus partition key."""
    default_domain: str = "eng"
    """Fallback partition when topic_to_domain lookup misses."""

    # Logging
    log_level: str = "info"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


def get_model(role: str, overrides: dict | None = None) -> str:
    """Return model tag: override > env var > default."""
    if overrides and overrides.get(role):
        return overrides[role]
    return getattr(settings, role)
