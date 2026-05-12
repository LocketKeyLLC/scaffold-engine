"""Scaffold Engine configuration — loaded from environment variables."""
import logging

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings

_logger = logging.getLogger("scaffold.config")


# ---------------------------------------------------------------------------
# DAG validation enums (#101)
# ---------------------------------------------------------------------------
VALID_TASK_TYPES = frozenset({"research", "decision", "action", "validation", "output"})
VALID_STRATEGIES = frozenset({"sequential", "parallel", "hybrid", "conditional"})
VALID_TOOLS = frozenset({"LLM", "CodeGen", "SearXNG", "Milvus"})
VALID_DOMAINS = frozenset({"prompt", "rag", "eng", "llm", "spec", "code", "qa"})

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
    "release_notes": 365 * 86400,
    "test_code": 365 * 86400,
    "ci_config": 365 * 86400,
    "model_card": 365 * 86400,
    "dataset_card": 365 * 86400,
    "paper_abstract": 730 * 86400,
    "so_answer": 90 * 86400,
    "reddit_post": 90 * 86400,
    "hn_comment": 90 * 86400,
    "wiki_article": 180 * 86400,
    # §17.125 — disputed_claim: downvoted / locked / withdrawn forum
    # content ingested as negative knowledge. Short-ish TTL since the
    # underlying content may be edited/deleted by upstream moderation.
    "disputed_claim": 60 * 86400,
}
DEFAULT_TTL_SECONDS = 180 * 86400


class Settings(BaseSettings):
    """All config sourced from env vars set in docker-compose.yml."""

    # Auth
    scaffold_api_key: SecretStr = SecretStr("")
    scaffold_auth_disabled: bool = False

    # Sprint J.2 — native web UI loopback (HTTP-loopback so the SDK gets
    # dogfooded as the second consumer after CLI). Override via env when
    # running the orchestrator on a non-default port or behind a proxy.
    web_loopback_url: str = "http://localhost:8000"
    web_loopback_timeout: int = 30
    # J.2.b — separate timeout for long-running calls (ideate Phase 1 100-
    # 547s; ideate/confirm Phase 2 512-1450s). The web routes that fire
    # these in BackgroundTasks instantiate a second Client with this
    # timeout so the read path's 30s ceiling stays in place.
    web_loopback_long_timeout: int = 1800

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
    # §17.138 — On lifespan startup, SCAN up to N keys from Redis matching
    # the current embedder identity (model_id + dim) and populate the L1
    # LRU. Saves a Redis round-trip on every warm-cache query during the
    # first few minutes after restart. 0 disables. Capped per-call at
    # embedding_cache_memory_size regardless of the configured N.
    embedding_cache_warmup_n: int = Field(default=0, ge=0, le=100_000)

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
    # Switched from qwen3-embedding-8b-mrl512 in audit-tail Finding D
    # (§17.83) — that model wedged deterministically on this host's
    # Ollama 0.17.5 --ollama-engine path. nomic-embed-text is 137M
    # params (50× smaller), 768-dim native truncated to 512 via MRL.
    model_embedder_id: str = "nomic-embed-text-mrl512"
    semantic_dedup_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    version_chain_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    embedding_batch_size: int = Field(default=32, ge=1, le=512)

    # Model assignments
    model_router: str = "qwen3:4b"
    model_embedder_pipeline: str = "nomic-embed-text"
    model_reranker: str = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
    model_coder: str = "qwen2.5-coder:7b"
    model_general: str = "qwen3-vl:235b-instruct-cloud"
    # Ideation phase model role (Apr 26 2026): which ROLE_FIELDS entry to
    # use for analyze/distill/compile. "model_router" = local 4b (audit
    # #6.1 default, slower on CPU). "model_general" = cloud 235b (faster,
    # network required). Override: IDEATION_MODEL_ROLE.
    ideation_model_role: str = "model_general"
    # §17.144 — Spec-capture extractor role. Default model_general
    # because the extractor must follow the spec_schema.json contract
    # strictly (full JSON schema in the prompt, ~150 lines); smaller
    # local models tend to drift. Operators with strict offline
    # requirements can override to model_router or model_verifier.
    spec_extractor_model_role: str = "model_general"
    # §17.147 — Closed-loop device-sizing budget. The stage proposes
    # parameters, runs ngspice, feeds the measurement gap back to the
    # LLM, and repeats until convergence or the budget is exhausted.
    # Each iteration is one ngspice subprocess + one LLM round trip.
    # Default 3 is the working compromise: enough for analytical →
    # one refinement → safety net, without making a non-convergent
    # design wait for an expensive 10-iter futile loop.
    device_sizing_max_iterations: int = Field(default=3, ge=1, le=10)
    model_verifier: str = "qwen2.5:7b"
    model_cloud_heavy: str = "qwen3-vl:235b-instruct-cloud"
    model_cloud_alt: str = "qwen3.5:397b-cloud"
    model_fallback: str = "qwen3.5:latest"

    # Per-role provider routing (Sprint E). Each role names which backend
    # serves it; default "ollama" preserves pre-Sprint-E behavior. Override
    # with MODEL_<ROLE>_PROVIDER env vars. The reranker is exempt — it runs
    # as a CrossEncoder singleton outside the provider system. Any value
    # other than a registered provider raises ProviderError at call time
    # via app.providers.provider_for_role.
    model_general_provider: str = "ollama"
    model_verifier_provider: str = "ollama"
    model_coder_provider: str = "ollama"
    model_router_provider: str = "ollama"
    model_fallback_provider: str = "ollama"
    model_cloud_heavy_provider: str = "ollama"
    model_cloud_alt_provider: str = "ollama"
    model_embedder_pipeline_provider: str = "ollama"

    # OpenAI-compatible provider config (Sprint F). The base URL defaults to
    # api.openai.com but can be overridden to point at any OpenAI-compatible
    # endpoint (vLLM, LocalAI, Ollama OpenAI-mode, ...) — one provider, many
    # backends. The provider raises ProviderUnavailableError at call time if
    # the key is empty when it's actually needed, so leaving the key blank
    # while no role is bound to "openai" is fine.
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout: int = Field(default=600, ge=1, le=86400)
    openai_organization: str = ""

    # Timeouts (seconds)
    cloud_timeout: int = Field(default=3600, ge=1, le=86400)
    local_timeout: int = Field(default=1800, ge=1, le=86400)
    verify_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_retries: int = Field(default=3, ge=0, le=20)

    # §17.140 — ngspice sidecar (scaffold-ngspice). Reachable over the
    # ai-network bridge. The sidecar enforces its own per-run timeout
    # (caps the ngspice subprocess); the client timeout below is the
    # HTTP read-timeout safety net and must exceed the per-run cap.
    ngspice_url: str = "http://scaffold-ngspice:8001"
    ngspice_run_timeout_s: float = Field(default=30.0, gt=0.0, le=600.0)
    ngspice_http_timeout_s: float = Field(default=620.0, gt=0.0, le=3600.0)

    # §17.141 — Verilator sidecar (scaffold-verilator). Two timeouts:
    # one for the build phase (verilator + g++ compile) and one for the
    # simulation run. HTTP timeout must exceed (build + run) so the
    # sidecar always wins the timeout race and returns a typed result
    # rather than letting httpx raise ReadTimeout out from under us.
    verilator_url: str = "http://scaffold-verilator:8002"
    verilator_run_timeout_s: float = Field(default=60.0, gt=0.0, le=1800.0)
    verilator_build_timeout_s: float = Field(default=120.0, gt=0.0, le=1800.0)
    verilator_http_timeout_s: float = Field(default=2000.0, gt=0.0, le=7200.0)

    # §17.142 — SymbiYosys sidecar. Single timeout — sby's pipeline is
    # one synchronous run that internally drives yosys + the SMT solver.
    # HTTP timeout must exceed run_timeout_s (sby's own timeout)
    # comfortably so the sidecar's typed TIMEOUT verdict wins over
    # httpx ReadTimeout.
    symbiyosys_url: str = "http://scaffold-symbiyosys:8003"
    symbiyosys_run_timeout_s: float = Field(default=120.0, gt=0.0, le=3600.0)
    symbiyosys_http_timeout_s: float = Field(default=3700.0, gt=0.0, le=7200.0)

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
    github_max_issues: int = Field(default=25, ge=0, le=200)
    github_max_releases: int = Field(default=10, ge=0, le=100)
    github_max_discussions: int = Field(default=25, ge=0, le=200)
    github_min_issue_reactions: int = Field(default=2, ge=0, le=1000)
    github_blob_concurrency: int = Field(default=8, ge=1, le=64)
    github_timeout: int = Field(default=30, ge=1, le=300)
    github_api_base: str = "https://api.github.com"

    # Hugging Face Hub. Token optional — public model/dataset/paper/space
    # access works unauthenticated; token raises the rate limit ceiling.
    huggingface_token: str = ""
    huggingface_timeout: int = Field(default=30, ge=1, le=300)
    huggingface_api_base: str = "https://huggingface.co"
    # Audit M6 — Redis cache for /repos/{o}/{r}/git/trees/{b} responses.
    # Cache is keyed by (owner, repo, branch) and stores (etag, blobs,
    # truncated). On hit, an If-None-Match header is sent so GitHub can
    # return 304 (which doesn't count against the rate limit) when the
    # tree hasn't changed. 0 disables the cache (forces every call to
    # be a live API hit). Default 30 min covers the burst case (someone
    # iterating on `/research`) without holding entries forever.
    github_tree_cache_ttl_seconds: int = Field(default=1800, ge=0, le=86400)
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
    # §17.97 — global request body size cap (applies to all routes except
    # /research/pdf which has its own larger cap). 2 MB covers every
    # legitimate JSON body the orchestrator currently accepts; over that
    # is almost certainly a malformed or oversized payload.
    max_request_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    # §17.93 — SSRF guard knob. Default False rejects any /research url:
    # or /research openapi: target whose hostname resolves to a private,
    # loopback, link-local, reserved, or multicast IP. Flip to True ONLY
    # for local-development scenarios where the orchestrator legitimately
    # needs to fetch internal hosts (e.g. an in-cluster OpenAPI spec).
    research_allow_private_hosts: bool = False

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
    # Sprint X.1 — tightened from 7d to 72h (4320 min). 7d was generous
    # but in practice a 3-day stall on a pending confirmation almost
    # always means the operator forgot, and the stuck job is more
    # painful (cluttering /jobs list) than the rare case of a user
    # legitimately waiting longer. Range floor stays at 60 min so
    # operators with shorter SLAs can tighten further.
    awaiting_confirmation_stale_minutes: int = Field(default=4320, ge=60, le=43200)  # 72h default, max 30d
    # Assist Mode: an assist_session with last_activity_at older than this
    # is treated as abandoned and the owning job moves to 'cancelled'.
    # Long default (7d) because manual implementation legitimately spans
    # multiple working days.
    assist_idle_threshold_days: int = Field(default=7, ge=1, le=90)
    # Sprint W.5 — when policy='selective' fires (or 'full'), call the LLM
    # to rewrite prompt_template for affected downstream nodes so their
    # short execution hint reflects the new upstream output. Fail-open:
    # any LLM/parse failure falls back to the legacy reset-only behavior.
    # Disable via assist_replan_regen_enabled=false to skip the LLM call
    # entirely (cost-sensitive deployments, or when legacy behavior is
    # known to be sufficient).
    assist_replan_regen_enabled: bool = True
    assist_replan_regen_max_tokens: int = Field(default=2048, ge=512, le=8192)
    # #2 — orphan detection: dag_nodes stuck in 'running' past this threshold
    # are treated as orphaned (executor died) and reset to 'pending' for
    # automatic re-execution. Sprint X.1 tightened 60→30 min: the audit
    # flagged that a dead executor could leave a node stuck for nearly
    # an hour before recovery. 30 min still > worst observed single-node
    # duration (~30min) — the orphan reset puts the node back to
    # 'pending' (not 'failed'), so a legitimately-running node would
    # simply re-execute on the next /execute/all tick.
    node_orphan_threshold_minutes: int = Field(default=30, ge=5, le=1440)
    cleanup_interval_seconds: int = Field(default=900, ge=10, le=86400)

    # Execution agent tuning
    node_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    max_upstream_chars: int = Field(default=8000, ge=100, le=200000)
    rag_cosine_floor: float = Field(default=0.3, ge=0.0, le=1.0)
    verifier_top_k: int = Field(default=5, ge=1, le=50)
    compile_output_gate_chars: int = Field(default=50_000, ge=1_000, le=1_000_000)
    compile_output_min_chunk: int = Field(default=200, ge=1, le=10_000)
    # Sprint W.2 — content cap for the stored compiled_output. Distinct from
    # compile_output_gate_chars (which gates the SSE-transport payload).
    # Strategy 3 (concat-all-done-nodes fallback) truncates per-node
    # proportionally to fit; Strategies 0 + 2 produce a single deliverable
    # so the cap rarely binds for them. Default 100k chars handles typical
    # multi-node outputs; pathological cases (50+ verbose nodes) get clipped.
    compile_output_max_chars: int = Field(default=100_000, ge=1_000, le=2_000_000)
    # Sprint W.7 — opt-in LLM-driven post-processing pass on the compiled
    # output. Default OFF so existing job behavior is unchanged. When ON,
    # the heuristic body produced by _compile_output is fed to an LLM that
    # rewrites the sectioned dump into a coherent narrative. Fail-open:
    # any LLM/parse failure returns the heuristic body unchanged.
    # CodeGen-deliverable jobs (Strategy 2 with tool='CodeGen' source) skip
    # synthesis even when enabled — executable code passes through verbatim.
    compile_synthesis_enabled: bool = False
    compile_synthesis_max_tokens: int = Field(default=4096, ge=512, le=16384)
    # Sprint W.3 — DAG generator tool-pick validator. After the LLM emits a DAG,
    # a second-pass validator LLM checks each task's tool selection against the
    # documented rules and returns issues; if any are found, the generator is
    # re-prompted with strict corrections, up to dag_validator_max_retries times.
    # Disable the entire loop with dag_validator_enabled=false (falls back to
    # the legacy single-shot DAG generation).
    dag_validator_enabled: bool = True
    dag_validator_max_retries: int = Field(default=2, ge=0, le=5)
    dag_validator_max_tokens: int = Field(default=1024, ge=256, le=8192)
    execution_global_retry_cap: int = Field(default=20, ge=0, le=1000)
    # Sprint X.24 — process-wide cap on concurrent execute_all_nodes runs.
    # Each run drives its own inference loop and holds short-lived DB
    # sessions. N concurrent callers (HTTP /execute/all, assist-handoff,
    # scheduled jobs) can exhaust the SQLAlchemy pool (pool_size=5,
    # max_overflow=10) and cascade 500s. Default 1 keeps the single-user
    # invariant strict; raise once the pool is sized to match.
    execution_global_concurrency: int = Field(default=1, ge=1, le=32)
    # Max queue wait when the cap is full. 0 = wait forever; otherwise
    # the run emits a 503-shaped SSE error and bails. Default 1800s
    # matches scheduler_job_timeout so a queued run can't outlive the
    # scheduler that booked it.
    execution_queue_timeout_seconds: int = Field(default=1800, ge=0, le=86400)
    sse_keepalive_seconds: float = Field(default=15.0, ge=1.0, le=300.0)

    # Manual prompt-edit cap (POST /prompts/{job_id}/{node_key}). Both
    # the orchestrator-side update_prompt() and the OWUI prompt_inspector
    # pipeline pre-check against this value so the user sees a consistent
    # limit regardless of where the cap fires.
    prompt_max_chars: int = Field(default=16_384, ge=1024, le=1_000_000)

    # Logging — all three values were previously read directly via
    # ``os.getenv`` in app/main.py; centralizing here so logging config
    # flows through the same Settings layer as everything else.
    log_level: str = "info"
    log_json_format: bool = True
    log_file: str | None = None

    # Sprint X.26 — observability surface. Closes the §16.5 gap list:
    #   * /metrics (Prometheus) when metrics_enabled
    #   * file + DB alert sinks (alert_file_path, alert_db_enabled)
    #   * push X.20 thresholds (alert_eval_*, conservative defaults)
    #   * calibration cron failure + no-fire watchdog
    #   * env-gated OTel (off unless otel_enabled + endpoint set)
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"

    alert_file_path: str = ""              # empty disables file sink
    alert_db_enabled: bool = True
    alert_cooldown_seconds: int = Field(default=3600, ge=0, le=86400)

    alert_eval_enabled: bool = True
    alert_eval_interval_seconds: int = Field(default=300, ge=30, le=3600)
    alert_eval_window_minutes: int = Field(default=60, ge=1, le=1440)

    alert_unresolved_errors_threshold: int = Field(default=1, ge=0, le=10000)
    alert_cost_window_usd_threshold: float = Field(default=5.0, ge=0.0, le=100000.0)
    alert_p95_latency_ms_threshold: int = Field(default=120000, ge=0, le=3600000)

    # Embedding-cache pressure alert. Fires only when BOTH conditions hold
    # over a tick interval, so cold-start churn (high miss rate, zero
    # evictions) does not false-positive. Set either to 0 to disable.
    alert_embedding_evictions_threshold: int = Field(default=500, ge=0, le=1_000_000)
    alert_embedding_hit_rate_floor: float = Field(default=0.5, ge=0.0, le=1.0)

    calibration_watchdog_enabled: bool = True
    calibration_watchdog_interval_seconds: int = Field(default=900, ge=60, le=86400)
    calibration_grace_minutes: int = Field(default=120, ge=10, le=1440)

    otel_enabled: bool = False
    otel_service_name: str = "scaffold-engine"
    otel_otlp_endpoint: str = ""           # http://otel-collector:4318/v1/traces

    # Deep-search per-mode budget caps. Each producer caps how many
    # artifacts it fetches per /research invocation. GitHub budgets live
    # in the github_* block above (consolidated with existing settings).
    hf_max_files: int = Field(default=30, ge=1, le=200)
    so_max_answers: int = Field(default=20, ge=1, le=100)
    so_min_score: int = Field(default=10, ge=0, le=10000)
    reddit_max_posts: int = Field(default=20, ge=1, le=100)
    reddit_min_score: int = Field(default=50, ge=0, le=100000)
    reddit_min_comments: int = Field(default=10, ge=0, le=10000)
    # §17.125 — opt-in: when True, SO + Reddit forum modes also emit
    # below-gate items tagged source_type=disputed_claim (low confidence)
    # so retrieval can warn "commonly cited but disputed."
    forum_ingest_disputed: bool = False
    hn_max_items: int = Field(default=25, ge=1, le=200)
    hn_min_points: int = Field(default=100, ge=0, le=10000)
    arxiv_max_sections: int = Field(default=10, ge=1, le=50)
    wiki_max_pages: int = Field(default=10, ge=1, le=50)

    # Upstream HTTP cache (fetchv1: prefix in Redis). TTLs split by ref
    # mutability: SHA/revision-pinned → long; mutable → short. Body cap
    # mirrors the bounded-fetch limit (5 MB default).
    fetch_cache_ttl_default_seconds: int = Field(default=3600, ge=60, le=2592000)
    fetch_cache_ttl_immutable_seconds: int = Field(default=30 * 86400, ge=60, le=365 * 86400)
    fetch_cache_max_body_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    # Cardinality cap. The body-bytes cap above bounds individual entry
    # size; this bounds the key COUNT to prevent a /research over a
    # monorepo (e.g. github:huge/repo walking 50k files) from blowing
    # out Redis. 0 disables the check entirely. The count is sampled
    # via SCAN MATCH fetchv1:* at most once per fetch_cache_count_interval_s
    # — the cached count gates puts in between samples, so a burst at
    # most exceeds the cap by one interval's worth of writes.
    fetch_cache_max_keys: int = Field(default=50_000, ge=0, le=10_000_000)
    fetch_cache_count_interval_s: int = Field(default=30, ge=5, le=3600)

    # Verifier-verdict cache (llmverifyv1: prefix in Redis). Default OFF
    # because the verifier path is fail-closed and a stale cache hit could
    # mask a real regression. When ON, deterministic verifier calls
    # (temperature=0.0) skip the LLM when an identical (messages,
    # tool_schema, model) tuple was seen within the TTL window. Only
    # ``pass`` verdicts are cached — fails must re-run because W.1
    # feedback injection changes the retry prompt.
    cache_llm_responses: bool = False
    llm_response_cache_ttl_s: int = Field(default=3600, ge=60, le=30 * 86400)

    # RAG retrieval-result cache (ragv1: prefix in Redis). Default OFF
    # because retrieval-quality regressions are most visible on fresh
    # runs — a stale cache hit could mask a real drop. Short TTL (120 s)
    # when enabled, scoped to cover multi-node references to the same
    # query within a single job. Only ``status=ok`` responses without
    # warnings or below_threshold are cached.
    cache_rag_results: bool = False
    rag_result_cache_ttl_s: int = Field(default=120, ge=10, le=86400)
    rag_result_cache_max_value_bytes: int = Field(
        default=256 * 1024, ge=4 * 1024, le=5 * 1024 * 1024,
    )

    model_config = {"env_file": ".env", "extra": "ignore"}

    @model_validator(mode="after")
    def _warn_timeout_vs_reaper(self) -> "Settings":
        """If node_timeout_seconds >= stale_threshold_minutes*60 the reaper can
        mark a job 'failed' while a node is still mid-inference. Warn loudly at
        import time; don't fail startup since misconfig is recoverable.
        """
        reaper_seconds = self.stale_threshold_minutes * 60
        if self.node_timeout_seconds >= reaper_seconds:
            _logger.warning(
                "config_timeout_reaper_overlap: "
                "node_timeout_seconds=%d >= stale_threshold_minutes*60=%d — "
                "reaper may mark live jobs failed mid-execution. "
                "Lower node_timeout_seconds or raise stale_threshold_minutes.",
                self.node_timeout_seconds, reaper_seconds,
            )
        return self


settings = Settings()


def get_model(role: str, overrides: dict | None = None) -> str:
    """Return model tag: override > env var > default.

    Restricted to ROLE_FIELDS allowlist to prevent arbitrary attribute access.
    Empty-string overrides are rejected explicitly rather than silently
    falling through to the default — pass the role's setting tag, or omit
    the entry entirely.
    """
    if role not in ROLE_FIELDS:
        raise ValueError(
            f"unknown role {role!r}; must be one of {sorted(ROLE_FIELDS)}"
        )
    if overrides and role in overrides:
        override_value = overrides[role]
        if override_value is None:
            # Omit-the-key semantics: caller explicitly nulled the override.
            return getattr(settings, role)
        if not isinstance(override_value, str) or not override_value.strip():
            raise ValueError(
                f"override for role {role!r} must be a non-empty string"
            )
        return override_value
    return getattr(settings, role)
