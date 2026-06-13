"""Scaffold Engine configuration — loaded from environment variables."""
import logging
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings

# §17.349 — provider-name allowlist. Matches the registered providers in
# app/providers/__init__.py (ollama + openai + Sprint §17.345's anthropic).
# Used as a Literal type on all model_<role>_provider fields so typos in
# .env fail at orchestrator boot (ValidationError) instead of mid-pipeline
# at first call (ProviderError). When a new provider is added, update
# this constant AND the registry _autoload tuple in providers/__init__.py.
ProviderName = Literal["ollama", "openai", "anthropic"]

_logger = logging.getLogger("scaffold.config")


# ---------------------------------------------------------------------------
# DAG validation enums (#101)
# ---------------------------------------------------------------------------
VALID_TASK_TYPES = frozenset({"research", "decision", "action", "validation", "output"})
VALID_STRATEGIES = frozenset({"sequential", "parallel", "hybrid", "conditional"})
VALID_TOOLS = frozenset({"LLM", "CodeGen", "SearXNG", "Milvus", "Shell"})
VALID_DOMAINS = frozenset({"prompt", "rag", "eng", "eng_design", "llm", "spec", "code", "qa"})

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

# §17.483 — roles whose model can be re-pointed at runtime. The embedder and
# reranker are excluded: the embedding dim is locked at 512 (probed at startup)
# and the reranker is a CrossEncoder singleton — both are config-only and a
# live swap would corrupt indexing / break the loaded model. Everything else
# resolves through get_model() per request, so mutating settings.<role> takes
# effect immediately for orchestrator-initiated work.
_MODEL_SINGLETON_ROLES = frozenset({"model_embedder_pipeline", "model_reranker"})
SWITCHABLE_ROLE_FIELDS = ROLE_FIELDS - _MODEL_SINGLETON_ROLES


def set_runtime_model(role: str, model: str) -> None:
    """§17.483 — re-point a switchable role's model on the live settings
    singleton (ephemeral; a container restart reverts to env/.env).

    Mutates ``settings.<role>`` in-process. The orchestrator runs a single
    uvicorn worker, so the change is globally effective for any subsequent
    ``get_model(role)`` resolution that doesn't carry an explicit per-request
    override. Does NOT propagate to the OWUI pipeline valves (a separate
    process) — chat-launched jobs ship their own ``model_overrides``.

    Raises ``ValueError`` on a non-switchable role (singletons are config-only)
    or an empty/blank tag. The caller is responsible for validating the tag
    exists on the provider; this only guards the role + non-emptiness.
    """
    if role not in SWITCHABLE_ROLE_FIELDS:
        if role in _MODEL_SINGLETON_ROLES:
            raise ValueError(
                f"role {role!r} is config-only (embedder/reranker are "
                f"singletons) — set the env var and restart"
            )
        raise ValueError(
            f"unknown role {role!r}; must be one of "
            f"{sorted(SWITCHABLE_ROLE_FIELDS)}"
        )
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model tag must be a non-empty string")
    setattr(settings, role, model.strip())


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

    # Reranker tuning (moved from rag_pipeline.py module constants).
    # §17.233 — rerank_max_candidates default 32 → 10. CrossEncoder.predict
    # is quadratic in sequence length and linear in batch size, so total
    # rerank latency scales roughly linearly with this cap. Live observation
    # on T480 (i5-8350U, 4 cores) at default rerank_doc_truncate=2000 is
    # ~7 s per pair; 32 candidates ≈ 234 s/call (operator-unacceptable for
    # OWUI interactive use), 10 candidates ≈ 70 s/call. The output top_k
    # (typically 10) is unchanged — we just stop reranking RRF positions
    # 11-32, which is an at-the-margin quality cost (rerank's job is
    # reordering; the top-10 by RRF is usually a strong shortlist already).
    # Operators wanting the deeper rerank can raise via
    # RERANK_MAX_CANDIDATES in .env (no code change required).
    rerank_max_candidates: int = Field(default=10, ge=1, le=512)
    # §17.235 — rerank_doc_truncate default 2000 → 500. Empirical sweep
    # (scripts/eval_doc_truncate.py against tests/fixtures/golden_set.json,
    # KB=732 post-Tier-A + Truncation re-ingest) at max_candidates=10:
    #   truncate=2000  →  52.0 s/query   coverage@5/10=15% (3/20)
    #   truncate=1000  →  28.5 s/query   coverage@5/10=15% (3/20)
    #   truncate= 500  →  17.2 s/query   coverage@5/10=15% (3/20)
    # 3× /rag latency cut on this benchmark with no measurable quality
    # change — the 3 hits' matching content concentrates in the first
    # 500 chars of each doc. The 17 misses are corpus-content gaps
    # (§17.231 surface-form drift) unaffected by truncate. Worst case for
    # the new default is a long-form entry whose matching paragraph sits
    # past char 500; operators with such workloads can raise via
    # RERANK_DOC_TRUNCATE in .env (no code change required). For per-
    # request override, see §17.234 candidate D logged in §17.235.
    rerank_doc_truncate: int = Field(default=500, ge=100, le=20000)
    rerank_warn_ms: int = Field(default=30000, ge=0, le=60000)
    rerank_error_ms: int = Field(default=120000, ge=0, le=300000)
    # §17.431 — Milvus 2.5 native BM25 sparse retrieval. When True, the
    # hybrid keyword leg uses a real BM25 sparse-vector search (Milvus
    # tokenizes + scores via a BM25 Function on canonical_text) instead of
    # the naive `canonical_text like "%word%"` substring scan (no TF/IDF, no
    # index). Default False: requires a one-time toon_v2 migration to add the
    # sparse field + Function (scripts/migrate_toon_v2_bm25.py) — until then
    # the collection has no sparse field, so _keyword_search detects that and
    # falls back to the LIKE path even when this flag is True. Set True only
    # after migrating. See app/modules/rag_pipeline.py::_keyword_search.
    rag_bm25_enabled: bool = Field(default=False)
    # §17.433 — code-execution sandbox sidecar (scaffold-coderunner). Base URL
    # the orchestrator uses to run untrusted LLM-generated code + its tests in
    # isolation (the software-path ground-truth oracle). Empty (default) =
    # disabled: app.sandbox.run_code() short-circuits with no network call, and
    # the sidecar is a non-default compose profile so it isn't started until an
    # operator opts in (CODERUNNER_URL=http://scaffold-coderunner:8010). See
    # docker/coderunner/ + app/sandbox/client.py.
    coderunner_url: str = ""
    # §17.434 — run a sandbox exec-smoke of CodeGen node output during
    # verification (execute the module top-level to catch runtime/import errors
    # the ast.parse gate + LLM verifier miss). Requires coderunner_url set AND
    # this flag True; both default off, so the verify path is unchanged until an
    # operator brings up the sandbox + opts in. Fail-soft: only a genuine
    # runtime error fails the node. See app/sandbox/codegen_check.py.
    codegen_execution_check_enabled: bool = False

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
    # §17.346 — model_router / model_coder / model_verifier defaults flipped
    # to the same cloud model that §17.344 chose for triage. Justified per-role:
    #   model_router: same model + same arg as §17.344 — cloud 287× faster + better discipline
    #   model_coder:  verified A/B on a CodeGen-shape task (line-count CLI) —
    #                 cloud 21× faster (2.4s vs 50.8s on this CPU) AND followed
    #                 the "no markdown fences" instruction that the specialized
    #                 qwen2.5-coder:7b ignored. The specialized-coder advantage
    #                 didn't materialize on this workload shape.
    #   model_verifier: §17.344 reasoning extends — judgment-heavy ("is X correct?"),
    #                   larger model wins, latency benefits identical.
    # model_fallback stays local on purpose — fallback should be DIFFERENT from
    # primary to actually help when primary fails (cloud → cloud fallback gives
    # no failure-mode diversity).
    model_router: str = "qwen3.5:397b-cloud"
    model_embedder_pipeline: str = "nomic-embed-text"
    model_reranker: str = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
    model_coder: str = "qwen3.5:397b-cloud"
    model_general: str = "qwen3.5:397b-cloud"
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
    model_verifier: str = "qwen3.5:397b-cloud"
    model_cloud_heavy: str = "qwen3.5:397b-cloud"
    model_cloud_alt: str = "qwen3.5:397b-cloud"
    model_fallback: str = "qwen3.5:latest"

    # Per-role provider routing (Sprint E). Each role names which backend
    # serves it; default "ollama" preserves pre-Sprint-E behavior. Override
    # with MODEL_<ROLE>_PROVIDER env vars. The reranker is exempt — it runs
    # as a CrossEncoder singleton outside the provider system. Any value
    # other than a registered provider raises ProviderError at call time
    # via app.providers.provider_for_role.
    # §17.349 — typed as Literal[ProviderName] so a typo in .env
    # (e.g. MODEL_GENERAL_PROVIDER=anthrpoic) fails Pydantic validation
    # at orchestrator boot with the full list of valid choices, instead
    # of silently returning ProviderError at first dispatch.
    model_general_provider: ProviderName = "ollama"
    model_verifier_provider: ProviderName = "ollama"
    model_coder_provider: ProviderName = "ollama"
    model_router_provider: ProviderName = "ollama"
    model_fallback_provider: ProviderName = "ollama"
    model_cloud_heavy_provider: ProviderName = "ollama"
    model_cloud_alt_provider: ProviderName = "ollama"
    model_embedder_pipeline_provider: ProviderName = "ollama"

    # OpenAI-compatible provider config (Sprint F). The base URL defaults to
    # api.openai.com but can be overridden to point at any OpenAI-compatible
    # endpoint (vLLM, LocalAI, Ollama OpenAI-mode, ...) — one provider, many
    # backends. The provider raises ProviderUnavailableError at call time if
    # the key is empty when it's actually needed, so leaving the key blank
    # while no role is bound to "openai" is fine.
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout: int = Field(default=600, ge=1, le=86400)

    # Anthropic provider config (§17.345). Key blank by default — provider
    # raises ProviderUnavailableError at call time only if a role is bound
    # to "anthropic" while the key is empty, mirroring OpenAIProvider.
    # ``anthropic_version`` pins the API version header (current GA value
    # since 2023; bump only when migrating to a new API version with a
    # tested code path). ``anthropic_prompt_caching`` toggles automatic
    # ephemeral caching of the system block — on by default because this
    # is a high-volume routing path; turn off if you need byte-identical
    # request shape (e.g. for replay).
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_timeout: int = Field(default=600, ge=1, le=86400)
    anthropic_version: str = "2023-06-01"
    anthropic_prompt_caching: bool = True
    openai_organization: str = ""

    # Timeouts (seconds)
    cloud_timeout: int = Field(default=3600, ge=1, le=86400)
    local_timeout: int = Field(default=1800, ge=1, le=86400)
    verify_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_retries: int = Field(default=3, ge=0, le=20)
    # §17.428 — deterministic Python-syntax gate for CodeGen node output.
    # Runs before the (lenient) LLM verifier; a fenced ```python block that
    # fails ast.parse downgrades the node to 'failed' so the retry loop
    # surfaces the SyntaxError. Default on (fail-closed posture); flip to
    # False via CODEGEN_SYNTAX_GATE_ENABLED to disable. See
    # app/modules/execution_codegen_gate.py.
    codegen_syntax_gate_enabled: bool = Field(default=True)
    # §17.429 — route CodeGen nodes through a stricter, code-reviewer verifier
    # (semantics + completeness + upstream-signature consistency + brief-spec
    # coverage) instead of the generic lenient presence-checker. Gets the brief
    # goal + upstream sibling code as context. Default on; flip to False via
    # CODEGEN_VERIFIER_STRICT to fall back to the generic verifier if it ever
    # over-rejects. See app/modules/execution_verify._verify_codegen_output.
    codegen_verifier_strict: bool = Field(default=True)

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

    # §17.414 — formal-verify stage (symbiyosys-in-the-loop). Closed-loop
    # repair budget mirrors device_sizing_max_iterations; mode/depth are the
    # sby defaults the stage passes through to run_symbiyosys.
    formal_verify_max_iterations: int = Field(default=3, ge=1, le=10)
    formal_verify_mode: str = "bmc"
    formal_verify_depth: int = Field(default=20, ge=1, le=200)

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
    # §17.448 (Phase B / B1) — RAGAS-inspired faithfulness scoring of research
    # summaries against the collected sources. Default-OFF: it adds one LLM
    # tool-call per research run (cost), and is fail-soft so flag-off = unchanged
    # behaviour. faithfulness_model_role picks which role scores it.
    faithfulness_check_enabled: bool = False
    faithfulness_model_role: str = "model_verifier"
    # §17.452 (Phase C) — Chain-of-Verification revision of research summaries
    # (draft → verification questions → independent answers → revise). Where
    # faithfulness *scores*, CoVe *corrects*. Default-OFF: it adds ~3 LLM calls
    # per research run (cost); fail-soft, so flag-off = unchanged behaviour.
    cove_check_enabled: bool = False
    cove_model_role: str = "model_verifier"
    # §17.406 — bound the CPU-bound pypdf/pdfplumber extract so a corrupt or
    # adversarially large PDF can't hang the research session indefinitely.
    # wait_for cancels the awaiting coroutine on timeout (the off-loop thread
    # keeps running until the lib returns — Python can't kill threads — but the
    # session fails cleanly instead of blocking forever).
    research_pdf_extract_timeout: int = Field(default=120, ge=1, le=600)
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
    # §17.442 — cap raised 1440 → 2880 (48h). node_timeout_seconds caps at
    # 86400s (24h); with the old 1440-min (24h) cap the reaper window could only
    # ever EQUAL a max node_timeout, never exceed it, so a job running a single
    # ~24h node raced the reaper (the `config_timeout_reaper_overlap` warning).
    # The higher cap lets an operator set the reaper window strictly above the
    # node timeout (this host: STALE_THRESHOLD_MINUTES=1560 = 26h, 2h margin).
    stale_threshold_minutes: int = Field(default=30, ge=1, le=2880)
    # §17.442 — caps raised 1440 → 2880 in lockstep with stale_threshold_minutes.
    # The reaper hierarchy invariant requires planning/long_phase >= stale, so
    # lifting stale above a 24h node_timeout means these must be liftable too.
    planning_stale_minutes: int = Field(default=60, ge=1, le=2880)
    long_phase_stale_minutes: int = Field(default=45, ge=1, le=2880)
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
    # §17.486 — Assist Mode guidance layer. When a human claims a step, the
    # engine generates a human-executable walkthrough (copy-paste terminal
    # commands for shell/codegen work, step-by-step instructions for
    # non-coding work) instead of showing only the raw LLM prompt_template.
    #   assist_auto_guide          — generate on every /assist next (cached;
    #                                a re-view does not re-spend). Off = the
    #                                walkthrough is generated only on demand
    #                                via /assist guide.
    #   assist_guide_research      — run a SearXNG/Milvus pre-pass to confirm
    #                                unknowns (versions, flags, package names)
    #                                and cite them. Fail-soft: any failure
    #                                degrades to guidance without research.
    #   assist_guide_model_role    — role resolved by model_router (cloud /
    #                                thinking model by default, like the
    #                                executor). Server-authoritative — not a
    #                                per-request override.
    #   assist_guide_max_tokens    — generous so a thinking model's reasoning
    #                                budget does not starve the content (the
    #                                §17.465 empty-content failure mode).
    assist_auto_guide: bool = True
    assist_guide_research: bool = True
    assist_guide_model_role: str = "model_general"
    assist_guide_max_tokens: int = Field(default=8192, ge=512, le=16384)
    assist_guide_max_research_queries: int = Field(default=3, ge=0, le=8)
    # §17.487 — Tier 1 "close the loop". On /assist submit, judge whether the
    # pasted evidence shows the step actually succeeded (catches a pasted
    # error/traceback being recorded as success). Adds one sync tool_call per
    # submit; disable to skip it. When assist_block_on_failed_verify is also
    # true, a 'failed' verdict does NOT mark the node done — the step stays
    # claimable until a clean re-submit. Default off (a false-negative verdict
    # could wrongly hold a real success); the verdict is always surfaced so the
    # user can act on it regardless.
    assist_verify_on_submit: bool = True
    assist_block_on_failed_verify: bool = False
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
    # §17.198 — startup pre-migration sweep: how long a 'running'
    # research_sessions row must have been idle (last updated_at) before
    # the boot-time sweep cancels it. Previously hardcoded 5 minutes
    # inside _pre_migration_sweep; the default keeps the prior behavior
    # while letting an operator restart-during-a-slow-LLM-call raise
    # the cutoff so the in-flight row doesn't get reaped mid-flight.
    # Bounds: 1 minute floor (anything less reaps healthy in-flight
    # rows); 1440-minute ceiling (24h — a long-running research session
    # is the only legitimate reason to need a value this high).
    startup_sweep_research_idle_min: int = Field(default=5, ge=1, le=1440)

    # Execution agent tuning
    node_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    # §17.465 — token budget for a node's generation call. The default
    # model_router.chat() cap is 4096; for a thinking model (qwen3.5:397b-cloud,
    # the cloud default since §17.440) num_predict is a SHARED budget for
    # reasoning + visible content. Reasoning alone routinely runs 5k-8k tokens,
    # so a 4096 cap leaves the answer empty (done_reason=length, content="") or
    # truncated mid-step — which the verifier rightly rejects, burning every
    # W.1 retry on a budget problem the prompt-layer retry cannot fix (this is
    # what blocked job 4e3b8f01 nodes T3/T5). 8192 gives reasoning + a full
    # answer room to coexist; live-measured a heavy passthrough/ZFS node lands
    # at ~4.3k completion tokens with content present. Mirrors the CoVe
    # _ANSWER_TOKENS=8192 live-tuning (§17.453).
    node_generation_max_tokens: int = Field(default=8192, ge=512, le=16384)
    # §17.465 — generation-layer redraws on a success+empty draw, BEFORE the
    # verifier/W.1 retry. Thinking-model emptiness is sampling variance: a fresh
    # draw almost always lands non-empty. Cheaper than a full node retry
    # (re-optimize + re-verify) and it does not consume a retry_count slot.
    node_generation_max_draws: int = Field(default=3, ge=1, le=5)
    max_upstream_chars: int = Field(default=8000, ge=100, le=200000)
    # §17.477 (Phase 3) — when over max_upstream_chars, allocate each upstream
    # node's surviving char budget by (verifier confidence × length) instead of
    # length alone, so higher-confidence upstream context is preserved. NULL
    # confidence (un-verified / skipped-verify nodes) is treated as 0.5. The
    # per-node confidence annotation on section headers ships unconditionally;
    # this flag only gates the truncation-budget weighting. Disable to fall
    # back to plain proportional-by-length truncation.
    upstream_confidence_ranking_enabled: bool = True
    rag_cosine_floor: float = Field(default=0.3, ge=0.0, le=1.0)
    verifier_top_k: int = Field(default=5, ge=1, le=50)
    # §17.188 — cap for ``_lookup_superseded`` so a brief-flood scenario
    # (filtered result count × 4) can't unboundedly inflate the Milvus
    # query limit. 128 is generous: typical retrieval returns ≤ 5 results
    # and the lookup queries entry_ids * 4 = 20; the cap only fires when
    # an unusually-large top_k or a future per-entry-version expansion
    # pushes past it. When fired, a structured log line surfaces so the
    # operator can decide whether to raise the cap.
    max_supersedes_lookup_results: int = Field(default=128, ge=1, le=10_000)
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
    # §17.476 (Phase 2) — dependency-completeness / dead-end detection. A
    # substantive node whose output neither feeds nor is fed by any
    # is_deliverable node is an orphan branch (the §17.471-474 defect). When
    # enabled, the generator flags orphans in the validator retry loop so the
    # model re-decomposes; any survivors are auto-linked to the primary
    # deliverable as a deterministic last resort. Disable to skip the check.
    dag_dead_end_check_enabled: bool = True
    execution_global_retry_cap: int = Field(default=20, ge=0, le=1000)
    # Sprint X.24 — process-wide cap on concurrent execute_all_nodes runs.
    # Each run drives its own inference loop and holds short-lived DB
    # sessions. N concurrent callers (HTTP /execute/all, assist-handoff,
    # scheduled jobs) can exhaust the SQLAlchemy pool and cascade 500s.
    # §17.340 — default raised 1 → 2 after the pool was sized to match
    # (database.py: pool_size=10, max_overflow=20) and host Ollama gained
    # NUM_PARALLEL=4 capacity. End-to-end verified at N=2 by
    # scripts/verify_concurrent_exec.py: queued_events=0, parallelism=1.997,
    # batch wall-clock -39 % vs cap=1 baseline (1729 s -> 1058 s), zero pool
    # errors, zero cross-pollution. Per-job exec time roughly doubles on this
    # CPU-bound host — cap=2 is the empirical sweet spot, cap=3+ scales
    # poorly (each additional concurrent job further carves the cores).
    # Operators on stronger inference hardware can raise via env override.
    execution_global_concurrency: int = Field(default=2, ge=1, le=32)
    # §17.442 — bound concurrent ideation requests (/ideas + /ideate). Unlike
    # execution, ideation had NO cap: the §17.441 stress test fired 6 concurrent
    # /ideate and all 6 hit the cloud at once (latency 33→81 s). The cap queues
    # bursts instead — acquired at the router layer so jobs aren't even created
    # until a slot frees. Default 4 (ideation is cloud-bound, not CPU-bound like
    # execution, so a higher cap than execution's 2 is fine).
    ideation_global_concurrency: int = Field(default=4, ge=1, le=32)
    # Max queue wait when the cap is full. 0 = wait forever; otherwise
    # the run emits a 503-shaped SSE error and bails. Default 1800s
    # matches scheduler_job_timeout so a queued run can't outlive the
    # scheduler that booked it.
    execution_queue_timeout_seconds: int = Field(default=1800, ge=0, le=86400)
    sse_keepalive_seconds: float = Field(default=15.0, ge=1.0, le=300.0)

    # §17.359 — Shell tool is a seam, not an implementation. When False
    # (default), DAG nodes tagged tool='Shell' route through the LLM
    # executor with a runbook-style system prompt (instructions for the
    # human to run; no past-tense fake-execution narration). When True,
    # the executor expects a real shell backend wired in execute_next_node
    # — until that lands, the Shell branch raises NotImplementedError so
    # flipping the flag without an implementation fails loudly rather than
    # silently downgrading.
    shell_tool_enabled: bool = Field(default=False)

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

    # §17.386 — /health window for the OOM-event summary. Aggregates
    # `system_alerts` rows with kind='container.oom_killed' (written by
    # the §17.161 host-side oom_watcher) into per-container counts +
    # most-recent timestamp. Set to 0 to disable the /health block
    # (the underlying alerts still land in the DB).
    oom_alerts_health_window_hours: int = Field(default=24, ge=0, le=720)

    # §17.388 — per-kind dedup-cooldown override. `alert_cooldown_seconds`
    # is the default for every kind; entries in this dict override it for
    # named kinds. Useful when one kind has a different cadence than the
    # rest — e.g., `host.oom_killed` may want a tighter cooldown than the
    # default 1 h so a multi-victim host OOM episode produces one alert per
    # comm without suppressing distinct victims, while
    # `calibration.no_fire` keeps the default to avoid notification storms
    # on the quarterly cron.
    #
    # JSON-parseable env: ALERT_KIND_COOLDOWNS='{"host.oom_killed":300,"calibration.no_fire":86400}'
    #
    # Values are clamped to [0, 86400] (same range as the scalar default)
    # by the model_validator below. 0 disables dedup for that kind (every
    # emit lands a row); 86400 is one day. Empty dict (the default) means
    # "no overrides — every kind uses alert_cooldown_seconds."
    alert_kind_cooldowns: dict[str, int] = Field(default_factory=dict)

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
    def _validate_alert_kind_cooldowns(self) -> "Settings":
        """§17.388 — clamp per-kind cooldown overrides to [0, 86400].

        Pydantic v2's ``Field(ge=..., le=...)`` constraint applies to
        scalar fields but not to dict values. Validate-after-load
        enforces the same range as the scalar default
        (`alert_cooldown_seconds`'s Field constraint) for every
        per-kind override. Out-of-range values are clamped (not
        rejected) so a typo in env var doesn't crash the orchestrator
        at boot — the operator sees a warning instead and the kind
        gets the nearest valid value.
        """
        clamped: dict[str, int] = {}
        for kind, seconds in (self.alert_kind_cooldowns or {}).items():
            if not isinstance(seconds, int):
                _logger.warning(
                    "config_alert_kind_cooldowns_bad_value: kind=%r value=%r — "
                    "must be int; dropping override",
                    kind, seconds,
                )
                continue
            if seconds < 0:
                _logger.warning(
                    "config_alert_kind_cooldowns_negative: kind=%r value=%d — "
                    "clamping to 0",
                    kind, seconds,
                )
                seconds = 0
            elif seconds > 86400:
                _logger.warning(
                    "config_alert_kind_cooldowns_over_cap: kind=%r value=%d — "
                    "clamping to 86400",
                    kind, seconds,
                )
                seconds = 86400
            clamped[kind] = seconds
        # Replace via object.__setattr__ because pydantic models are
        # mutable post-validation but the field still goes through the
        # __setattr__ machinery.
        object.__setattr__(self, "alert_kind_cooldowns", clamped)
        return self

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

# §17.484 — snapshot the env/config-seeded model tag for every switchable role
# BEFORE any runtime override is applied. `set_runtime_model` mutates the live
# settings singleton (and §17.484 persists the choice to the model_overrides
# table, reloaded at startup), so the original .env value would otherwise be
# unrecoverable. `clear_runtime_model` / the web "reset to env" path restore
# from this snapshot. Captured here, at module load, when settings.<role> still
# holds the pristine env value.
_ENV_MODEL_DEFAULTS = {role: getattr(settings, role) for role in SWITCHABLE_ROLE_FIELDS}


def env_default_model(role: str) -> str:
    """The env/config-seeded model tag for ``role`` (pre-override), used to
    show "vs default" in the UI and to revert on reset. Raises on a
    non-switchable role."""
    if role not in _ENV_MODEL_DEFAULTS:
        raise ValueError(
            f"unknown switchable role {role!r}; must be one of "
            f"{sorted(SWITCHABLE_ROLE_FIELDS)}"
        )
    return _ENV_MODEL_DEFAULTS[role]


def clear_runtime_model(role: str) -> None:
    """§17.484 — revert a switchable role to its env/config default (the
    inverse of ``set_runtime_model``). Raises on a non-switchable role."""
    if role not in _ENV_MODEL_DEFAULTS:
        raise ValueError(
            f"unknown switchable role {role!r}; must be one of "
            f"{sorted(SWITCHABLE_ROLE_FIELDS)}"
        )
    setattr(settings, role, _ENV_MODEL_DEFAULTS[role])


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
