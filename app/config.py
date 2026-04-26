"""Scaffold Engine configuration — loaded from environment variables."""
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


# ---------------------------------------------------------------------------
# DAG validation enums (#101)
# ---------------------------------------------------------------------------
VALID_TASK_TYPES = frozenset({"research", "decision", "action", "validation", "output"})
VALID_STRATEGIES = frozenset({"sequential", "parallel", "hybrid", "conditional"})
VALID_TOOLS = frozenset({"LLM", "CodeGen", "SearXNG", "Milvus"})
VALID_DOMAINS = frozenset({"prompt", "rag", "eng", "llm", "spec"})

# get_model() allowlist — prevents arbitrary attribute access via role string
ROLE_FIELDS = frozenset({
    "model_router",
    "model_embedder_pipeline",
    "model_reranker",
    "model_coder",
    "model_general",
    "model_verifier",
    "model_cloud_heavy",
    "model_cloud_alt",
    "model_fallback",
})

# ---------------------------------------------------------------------------
# TTL policy by source_type (seconds)
# ---------------------------------------------------------------------------
TTL_POLICY = {
    "real_time": 7 * 86400,
    "news": 30 * 86400,
    "community": 90 * 86400,
    "tech_docs": 180 * 86400,
    "curated": 365 * 86400,
    "official_docs": 365 * 86400,
    "ai_generated": 180 * 86400,
}
DEFAULT_TTL_SECONDS = 180 * 86400


class Settings(BaseSettings):
    """All config sourced from env vars set in docker-compose.yml."""

    # Auth
    scaffold_api_key: SecretStr = SecretStr("")
    scaffold_auth_disabled: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://scaffold:scaffold_dev_pw@scaffold-postgres:5432/scaffold_engine"

    @property
    def sync_database_url(self) -> str:
        """Sync DSN for APScheduler's SQLAlchemyJobStore (no asyncpg)."""
        prefix = "postgresql+asyncpg://"
        assert self.database_url.startswith(prefix), (
            f"database_url must start with {prefix!r}, got: {self.database_url!r}"
        )
        return self.database_url.replace(prefix, "postgresql+psycopg2://", 1)

    # Scheduler
    scheduler_enabled: bool = True
    scheduler_timezone: str = "UTC"
    scheduler_jobstore_url: str = ""
    scheduler_job_timeout: int = Field(default=3600, ge=1, le=86400)
    scheduler_misfire_grace_time: int = Field(default=300, ge=0, le=86400)
    scheduler_shutdown_timeout: int = Field(default=30, ge=0, le=600)

    # External services
    ollama_base_url: str = "http://172.18.0.1:11434"
    milvus_uri: str = "http://milvus-standalone:19530"
    milvus_num_partitions: int = Field(default=64, ge=1, le=4096)
    embedding_cache_memory_size: int = Field(default=10_000, ge=0, le=1_000_000)
    embedding_cache_ttl_s: int = Field(default=30 * 86400, ge=0, le=365 * 86400)

    # Reranker prompt template
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

    # Reranker tuning (moved from rag_pipeline.py module constants)
    rerank_max_candidates: int = Field(default=32, ge=1, le=512)
    rerank_doc_truncate: int = Field(default=2000, ge=100, le=20000)
    rerank_warn_ms: int = Field(default=30000, ge=0, le=60000)
    rerank_error_ms: int = Field(default=120000, ge=0, le=300000)

    searxng_url: str = "http://searxng:8080"
    redis_url: str = "redis://scaffold-redis:6379/0"

    # Embedding config
    embedding_dim: int = Field(default=512, ge=512, le=512)
    model_embedder_id: str = "qwen3-embedding-8b-mrl512"
    semantic_dedup_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    version_chain_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    embedding_batch_size: int = Field(default=32, ge=1, le=512)

    # Model assignments
    model_router: str = "qwen3:4b"
    model_embedder_pipeline: str = "qwen3-embedding:8b"
    model_reranker: str = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
    model_coder: str = "qwen2.5-coder:7b"
    model_general: str = "qwen3-vl:235b-instruct-cloud"
    # Ideation phase model role (Apr 26 2026): which ROLE_FIELDS entry to
    # use for analyze/distill/compile. "model_router" = local 4b (audit
    # #6.1 default, slower on CPU). "model_general" = cloud 235b (faster,
    # network required). Override: IDEATION_MODEL_ROLE.
    ideation_model_role: str = "model_general"
    model_verifier: str = "qwen2.5:7b"
    model_cloud_heavy: str = "qwen3-vl:235b-instruct-cloud"
    model_cloud_alt: str = "qwen3.5:397b-cloud"
    model_fallback: str = "qwen3.5:latest"

    # Timeouts (seconds)
    cloud_timeout: int = Field(default=3600, ge=1, le=86400)
    local_timeout: int = Field(default=1800, ge=1, le=86400)
    verify_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_retries: int = Field(default=3, ge=0, le=20)

    # Research agent
    research_max_iterations: int = Field(default=3, ge=1, le=20)
    research_max_queries: int = Field(default=8, ge=1, le=50)
    ideation_max_queries: int = Field(default=5, ge=1, le=50)
    ideation_max_distill_results: int = Field(default=15, ge=1, le=200)
    research_max_urls_per_iteration: int = Field(default=20, ge=1, le=200)
    research_searxng_delay: float = Field(default=1.5, ge=0.0, le=60.0)
    research_chunk_size: int = Field(default=1500, ge=100, le=50000)
    research_timeout: int = Field(default=3600, ge=1, le=86400)
    github_token: str = ""
    github_max_files: int = Field(default=50, ge=1, le=1000)
    github_blob_concurrency: int = Field(default=8, ge=1, le=64)
    github_timeout: int = Field(default=30, ge=1, le=300)
    github_api_base: str = "https://api.github.com"
    openapi_max_endpoints: int = Field(default=200, ge=1, le=5000)
    openapi_max_params_per_endpoint: int = Field(default=50, ge=1, le=500)
    openapi_timeout: int = Field(default=30, ge=1, le=300)

    # GT pipeline
    gt_github_owner: str = "LocketKeyLLC"
    gt_github_repo: str = "smokieRAGs"
    gt_github_branch: str = "main"
    gt_stats_scan_limit: int = Field(default=16384, ge=1, le=16384)

    # Research agent — fetch caps & concurrency
    research_max_url_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    research_max_pdf_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=500 * 1024 * 1024)
    research_fetch_concurrency: int = Field(default=5, ge=1, le=100)
    research_fetch_timeout: int = Field(default=15, ge=1, le=300)
    research_url_fetch_timeout: int = Field(default=30, ge=1, le=300)
    research_heartbeat_interval: int = Field(default=8, ge=1, le=120)
    research_max_entry_chars: int = Field(default=8000, ge=100, le=100000)

    # Research agent — topic → Milvus partition domain
    topic_to_domain: dict[int, str] = {
        1: "llm",
        2: "rag",
        3: "eng",
        4: "eng",
        5: "eng",
        6: "eng",
    }
    default_domain: str = "eng"

    # Stale-job reaper
    stale_threshold_minutes: int = Field(default=30, ge=1, le=1440)
    planning_stale_minutes: int = Field(default=60, ge=1, le=1440)
    long_phase_stale_minutes: int = Field(default=45, ge=1, le=1440)
    cleanup_interval_seconds: int = Field(default=900, ge=10, le=86400)

    # Execution agent tuning
    node_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    max_upstream_chars: int = Field(default=8000, ge=100, le=200000)
    rag_cosine_floor: float = Field(default=0.3, ge=0.0, le=1.0)
    verifier_top_k: int = Field(default=5, ge=1, le=50)
    compile_output_gate_chars: int = Field(default=50_000, ge=1_000, le=1_000_000)
    compile_output_min_chunk: int = Field(default=200, ge=1, le=10_000)
    execution_global_retry_cap: int = Field(default=20, ge=0, le=1000)
    sse_keepalive_seconds: float = Field(default=15.0, ge=1.0, le=300.0)

    # Logging
    log_level: str = "info"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


def get_model(role: str, overrides: dict | None = None) -> str:
    """Return model tag: override > env var > default.

    Restricted to ROLE_FIELDS allowlist to prevent arbitrary attribute access.
    """
    if role not in ROLE_FIELDS:
        raise ValueError(
            f"unknown role {role!r}; must be one of {sorted(ROLE_FIELDS)}"
        )
    if overrides and overrides.get(role):
        return overrides[role]
    return getattr(settings, role)
