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
VALID_TOOLS = frozenset({"LLM", "CodeGen", "SearXNG", "Milvus", "Shell", "MCP"})
VALID_DOMAINS = frozenset({"prompt", "rag", "eng", "eng_design", "llm", "spec", "code", "qa"})

# get_model() allowlist — prevents arbitrary attribute access via role string
ROLE_FIELDS = frozenset({
    "model_router",
    "model_embedder_pipeline",
    "model_reranker",
    "model_coder",
    "model_general",
    "model_verifier",
    "model_research_extract",
    "model_cloud_heavy",
    "model_cloud_alt",
    "model_fallback",
    "model_triage",  # §17.791 — native conversational triage + /go synthesis
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
    # §17.702 — exemplar: a completed project's deliverable, ingested by the
    # learning flywheel. Operator-executed / high-grounding proven solutions, so
    # a long TTL like curated docs (also silences the per-ingest unknown-source
    # warning from get_ttl_for_source).
    "exemplar": 365 * 86400,
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
    # §17.807 — install-time multi-user option (set by the `make init` wizard).
    # When True, X-API-Key auth accepts, in addition to the master
    # scaffold_api_key (which stays valid as the admin key), any live named key
    # in the api_keys table (mig 066), matched by SHA-256 digest. When False
    # (default, single-user) the master key is the only accepted credential and
    # the api_keys table is never consulted. Keys are minted/revoked with
    # `make key-add` / `make key-revoke` (scripts/keyctl.py).
    multi_user_enabled: bool = False

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

    def research_max_urls_for_depth(self, depth: str) -> int:
        """§17.802 — depth-scaled per-iteration URL cap (shallow < medium < deep).
        Unknown/unset depth falls back to the medium tier."""
        return {
            "shallow": self.research_max_urls_shallow,
            "medium": self.research_max_urls_medium,
            "deep": self.research_max_urls_deep,
        }.get(depth, self.research_max_urls_medium)

    # §17.812 (audit M3) — refuse to serve when a startup migration fails.
    # Default False keeps the historical "log + boot on a partial schema"
    # behavior (unchanged for tests + existing installs); set true (fresh
    # installs / compose) to hard-fail instead of silently serving traffic
    # against a schema missing later migrations. Either way the failure is now
    # surfaced on /health.warnings and via a system alert (see app/main.py).
    fail_on_migration_error: bool = False

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
    # §17.608 — this promise is now honored end-to-end: previously the
    # CrossEncoder's internal _MAX_PAIRS=20 silently truncated any value
    # > 20, and the partial-result guard then disabled reranking entirely.
    # The reranker now scores the full shortlist this bound defines.
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
    #   model_coder:  §17.498 — A/B'd (scripts/model_ab.py) vs the generalist on
    #                 the 8 CodeGen goldens ×2 AFTER the §17.497 fair-scoring fix:
    #                 kimi-k2.7-code:cloud = 16/16, avg 2.9s (vs generalist 16/16,
    #                 avg 15.6s — ~5× faster, tight 1.5-5.9s, no thinking-model
    #                 latency outliers) AND followed the terse brief faithfully
    #                 (qwen3-coder-next was equally fast but OVER-elaborated by
    #                 parroting the CODEGEN prompt examples — rejected). So the
    #                 coder role now runs a coding-specialized model; the other
    #                 roles stay on the qwen3.5 generalist.
    #   model_verifier: §17.344 reasoning extends — judgment-heavy ("is X correct?"),
    #                   larger model wins, latency benefits identical.
    # model_fallback stays local on purpose — fallback should be DIFFERENT from
    # primary to actually help when primary fails (cloud → cloud fallback gives
    # no failure-mode diversity).
    model_router: str = "qwen3.5:397b-cloud"
    model_embedder_pipeline: str = "nomic-embed-text"
    model_reranker: str = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
    model_coder: str = "kimi-k2.7-code:cloud"  # §17.575 — reverted §17.572 (see docker-compose.yml MODEL_CODER, decisive)
    # §17.632 — was qwen3.5:397b-cloud; A/B'd (synthesis probe, 5 reps) →
    # deepseek-v4-pro:cloud is 3.4× faster (5.6s vs 19.2s) at equal reliability
    # (5/5 non-empty) and equal/better synthesis quality with clean punctuation.
    # Env-overridable (docker-compose MODEL_GENERAL is decisive — 3-site sync).
    model_general: str = "deepseek-v4-pro:cloud"
    # Ideation phase model role (Apr 26 2026): which ROLE_FIELDS entry to
    # use for analyze/distill/compile. "model_router" = local 4b (audit
    # #6.1 default, slower on CPU). "model_general" = the flagship cloud
    # generalist (faster, network required). Override: IDEATION_MODEL_ROLE.
    ideation_model_role: str = "model_general"
    # §17.144 — Spec-capture extractor role. Default model_general
    # because the extractor must follow the spec_schema.json contract
    # strictly (full JSON schema in the prompt, ~150 lines); smaller
    # local models tend to drift. Operators with strict offline
    # requirements can override to model_router or model_verifier.
    spec_extractor_model_role: str = "model_general"
    # §17.487 — the extractor runs a cloud thinking model (model_general =
    # qwen3.5:397b-cloud) whose num_predict is a shared reasoning+content
    # budget; the old 4096 cap starved long reasoning → success=True + empty
    # content (the §17.465 failure mode, observed live as the
    # test_spec_extractor_live empty-draw flake). Generous budget + a few
    # retry-on-empty draws via chat_until_nonempty.
    spec_extractor_max_tokens: int = Field(default=8192, ge=512, le=16384)
    spec_extractor_max_draws: int = Field(default=3, ge=1, le=6)
    # §17.489 — topology-select reuses spec_extractor_model_role (the cloud
    # thinking model) and feeds it large RAG-chunk prompts, so it hit the same
    # §17.465 empty-content failure mode (the test_topology_select_db live skip).
    # Its own budget/draw knobs since its prompts run larger than the extractor's.
    topology_select_max_tokens: int = Field(default=8192, ge=512, le=16384)
    topology_select_max_draws: int = Field(default=3, ge=1, le=6)
    # §17.494 — the remaining sim-pipeline LLM stages (formal_verify,
    # device_sizing, digital_sizing) reuse spec_extractor_model_role (the cloud
    # thinking model) at a bare 4096 cap — the same §17.465 empty-content
    # straggler class. Shared budget + retry-on-empty draws via
    # chat_until_nonempty (these three feed sim feedback to one judgment call).
    sim_stage_max_tokens: int = Field(default=8192, ge=512, le=16384)
    sim_stage_max_draws: int = Field(default=3, ge=1, le=6)
    # §17.147 — Closed-loop device-sizing budget. The stage proposes
    # parameters, runs ngspice, feeds the measurement gap back to the
    # LLM, and repeats until convergence or the budget is exhausted.
    # Each iteration is one ngspice subprocess + one LLM round trip.
    # Default 3 is the working compromise: enough for analytical →
    # one refinement → safety net, without making a non-convergent
    # design wait for an expensive 10-iter futile loop.
    device_sizing_max_iterations: int = Field(default=3, ge=1, le=10)
    # §17.567 — model_verifier qwen3.5:397b-cloud → kimi-k2.7-code:cloud after
    # an objective A/B (scripts/model_ab.py --task verifier, repeat=5 over the
    # verdict-match goldens): kimi matched the baseline's perfect accuracy
    # (30/30) at ~4.6× the speed (1.34s vs 6.12s) with native tool-calls (no
    # coax) and zero flakiness. The verifier runs per-node, so the latency win
    # compounds. (kimi was flaky on EXTRACTION (§17.566) but perfect on the
    # lenient presence-check verify task — per-task reliability differs.) Not in
    # tool_call_coax_models, so it uses the native path.
    model_verifier: str = "kimi-k2.7-code:cloud"
    # §17.548 — research extraction (record_entries tool call) points at a
    # tool-CAPABLE model (native tool_calls) rather than the thinking
    # model_verifier (qwen3.5, which never does — see §17.547), so the
    # native-first path in tool_call fires; coaxing still catches prose batches.
    # §17.566 — swapped kimi-k2.7-code:cloud → qwen3-coder-next:cloud after an
    # objective A/B (extraction goldens): kimi was FLAKY (~3/10 entries=0) while
    # qwen3-coder-next was 10/10 AND faster.
    # §17.631 — qwen3-coder-next:cloud was RETIRED by Ollama Cloud 2026-07-15
    # (HTTP 410 Gone); the role had been silently falling back to kimi (~3/10 on
    # the distill goldens — the flakiness §17.566 fixed had quietly returned). Re-A/B'd
    # the live Ollama Cloud catalog (scripts/model_ab.py --task extraction,
    # repeat=5 then repeat=15 tie-breaker across deepseek-v4-pro / glm-5.1 /
    # glm-5.2 / minimax-m3 / gpt-oss / nemotron / kimi-k2.6): glm-5.1:cloud won
    # at 30/30 AND fastest of the perfect-reliability models (5.9s; vs
    # minimax-m3 30/30@9.5s, deepseek-v4-pro 30/30@6.4s, qwen3.5 30/30@52.8s;
    # glm-5.2 was 28/30). NOT in tool_call_coax_models → native tool-call path.
    model_research_extract: str = "glm-5.1:cloud"
    model_cloud_heavy: str = "qwen3.5:397b-cloud"
    model_cloud_alt: str = "qwen3.5:397b-cloud"
    model_fallback: str = "qwen3.5:latest"

    # §17.547 — models that do NOT reliably emit native `tool_calls`. qwen3.5
    # thinking models put their answer in content/thinking and never populate
    # message.tool_calls over Ollama's /api/chat, so a 100% tool-call miss was
    # measured for research extraction (role=model_verifier=qwen3.5:397b-cloud).
    # model_router.tool_call routes any model whose id contains one of these
    # substrings (case-insensitive) through the JSON-coaxing fallback instead of
    # the native path, even though the Ollama provider advertises
    # supports_native_tools=True (that flag is provider-wide, not per-model).
    tool_call_coax_models: list[str] = Field(default_factory=lambda: ["qwen3.5"])
    # §17.547 — min token budget for coaxed tool calls on a thinking model. Such
    # models spend tokens reasoning before emitting the JSON, so a tight caller
    # budget (research extraction passes 1024) can be consumed by reasoning
    # alone → empty output → entries=0. Floor it so the JSON survives. Only
    # applied to tool_call_coax_models; other coaxed calls keep the caller value.
    tool_call_coax_min_tokens: int = Field(default=4096, ge=512, le=32768)

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
    model_research_extract_provider: ProviderName = "ollama"
    model_coder_provider: ProviderName = "ollama"
    # §17.791 — native triage/synthesis model (mirrors the OWUI pipeline's live
    # triage_model). A thinking model; the native path strips <think> and uses a
    # generous max_tokens so it doesn't return empty-after-strip.
    model_triage: str = "qwen3.5:397b-cloud"
    model_triage_provider: ProviderName = "ollama"
    # §17.791 — triage history window (turns). Pins every user turn (facts) +
    # the last N turns to bound CPU-only thinking-model latency. Mirror of the
    # pipeline valve triage_history_window.
    triage_history_window: int = Field(default=8, ge=1, le=100)
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

    # §17.773 — grammar-constrained decoding (structured outputs). When a caller
    # passes ``response_schema=`` to model_router.generate/chat (or uses
    # ``generate_json``), the JSON Schema is threaded to the backend's native
    # constraint (Ollama ``format``, OpenAI ``response_format`` json_schema,
    # Anthropic ``output_config.format``) so the model emits schema-valid JSON at
    # decode time instead of relying on post-hoc json_repair.
    #
    # The master valve is PROVIDER-AWARE (gated in model_router via each
    # provider's ``supports_structured_outputs`` flag): when ON, the constraint is
    # applied ONLY to backends that actually enforce it — OpenAI and Anthropic —
    # and silently dropped for others (cloud-proxied Ollama ignores it, per the
    # §17.773 live smoke), which then fall back to the json_repair path exactly as
    # before. So turning this on is safe: it "applies only when it bites."
    # Default OFF (conservative + no OpenAI/Anthropic key smoke yet); flip on once
    # a role is bound to OpenAI/Anthropic. When OFF the schema is dropped and
    # behavior is byte-identical to the pre-§17.773 path.
    structured_outputs_enabled: bool = Field(default=False)
    # §17.773 — opt-in override to ALSO apply the constraint on Ollama. Default
    # OFF because the cloud proxy ignores ``format`` (live smoke); flip ON only in
    # a deployment whose Ollama roles run LOCAL models (llama.cpp enforces GBNF
    # grammars). No-op unless ``structured_outputs_enabled`` is also ON.
    structured_outputs_ollama_enabled: bool = Field(default=False)

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
    # §17.686 — drastically raised for goal-completion depth: research runs until
    # gap-analysis reports full coverage OR the iteration ceiling, so higher caps
    # let a complex goal research to completion while a simple one still stops
    # early. iters 3→10, queries 12→40, urls/iter 30→100, ideation 5→15.
    research_max_iterations: int = Field(default=10, ge=1, le=20)
    research_max_queries: int = Field(default=40, ge=1, le=50)
    ideation_max_queries: int = Field(default=15, ge=1, le=50)
    ideation_max_distill_results: int = Field(default=15, ge=1, le=200)
    # §17.802 — per-iteration URL fetch cap, DEPTH-SCALED (was a flat 100).
    # Governs total fetch VOLUME per iteration (breadth / cost / wall-time); peak
    # memory is bounded separately by research_fetch_concurrency (§17.801). Lean
    # shallow base with increasing degrees for deeper runs. Overridable via
    # RESEARCH_MAX_URLS_{SHALLOW,MEDIUM,DEEP}. Resolve with research_max_urls_for_depth().
    research_max_urls_shallow: int = Field(default=30, ge=1, le=200)
    research_max_urls_medium: int = Field(default=60, ge=1, le=200)
    research_max_urls_deep: int = Field(default=90, ge=1, le=200)
    research_searxng_delay: float = Field(default=1.5, ge=0.0, le=60.0)
    # §17.549 — soft recency: append the current year to search queries that
    # don't already name one, biasing SearXNG toward fresh results without a
    # hard time_range filter. Set false to restore pre-§17.549 query text.
    research_recency_query_boost: bool = True
    # §17.543 — max concurrent SearXNG searches per iteration. The delay above
    # is held inside each slot as a cooldown, so effective request rate is
    # ~concurrency / delay. Keep small to stay polite to upstream engines.
    research_searxng_concurrency: int = Field(default=3, ge=1, le=8)
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
    # §17.801 — lowered 5 → 3. `_fetch_and_extract` holds this semaphore across
    # BOTH the HTTP fetch AND the trafilatura/lxml parse (whose in-RAM tree runs
    # many× the raw HTML), so peak fetch memory ≈ concurrency × (research_max_url_bytes
    # + parse overhead). At 5 × 5 MB pages the topic-mode fan-out spiked the 6 GB
    # orchestrator into a cgroup memory-kill mid-run (§17.800: exit 0 / OOMKilled
    # false / SSE severed / session orphaned). 3 cuts the peak ~40% while keeping
    # useful parallelism; overridable via RESEARCH_FETCH_CONCURRENCY.
    research_fetch_concurrency: int = Field(default=3, ge=1, le=100)
    research_fetch_timeout: int = Field(default=15, ge=1, le=300)
    research_url_fetch_timeout: int = Field(default=30, ge=1, le=300)
    # §17.448 (Phase B / B1) — RAGAS-inspired faithfulness scoring of research
    # summaries against the collected sources. Default-OFF: it adds one LLM
    # tool-call per research run (cost), and is fail-soft so flag-off = unchanged
    # behaviour. faithfulness_model_role picks which role scores it.
    faithfulness_check_enabled: bool = False
    faithfulness_model_role: str = "model_verifier"
    # §17.798 — citation faithfulness (per-citation ATTRIBUTION). Where B1
    # faithfulness scores a claim against the whole context blob, this checks
    # whether each inline `[n]` citation's SPECIFIC source supports the sentence
    # it's attached to (ALCE citation precision). Extends the same black-box /
    # fail-soft lineage as faithfulness + CoVe; wired into the eval gate
    # (score_retrieval.py --citation-faithfulness + the live gate test).
    # Default-OFF; one LLM judge-call per scored answer.
    citation_faithfulness_check_enabled: bool = False
    citation_faithfulness_model_role: str = "model_verifier"
    # §17.662 — after a research run, surface a small set of user-tailored
    # decision OPTIONS ("branch out into choices that suit the user's needs")
    # when the topic is decision-shaped. ONLY-WHEN-APPLICABLE: the model returns
    # has_options=false for a straightforward factual/single-answer topic, so no
    # choices are fabricated. One LLM tool-call per run; fail-soft (an error or a
    # disabled flag → no options block, unchanged summary). Default ON.
    research_options_enabled: bool = True
    research_options_model_role: str = "model_general"
    research_options_max: int = 4
    # §17.569 — grounding gate: faithfulness-score the SYNTHESIZED job
    # deliverable against the source node-work (the W.7 synthesis can introduce
    # claims not in the work — the §17.522 drift). Default ON, FLAG-ONLY: when
    # score < grounding_min_score it prepends a ⚠️ banner + records
    # jobs.metadata.grounding; it NEVER blocks delivery, and is fail-soft (a
    # scorer miss → no banner). Only runs on synthesized text (verbatim
    # CodeGen/Shell skip synthesis). Reuses faithfulness_model_role for scoring.
    grounding_gate_enabled: bool = True
    grounding_min_score: float = Field(default=0.7, ge=0.0, le=1.0)
    # §17.570 — grounding LOOP (detect → correct). Upgrades the §17.569 gate
    # from flag-only to self-correcting via CoVe (cove_revise).
    # grounding_correct_enabled (default ON): when the deliverable scores below
    # grounding_min_score, CoVe-revise it + re-score before deciding to banner —
    # so a low deliverable auto-corrects rather than just warning. Fail-soft.
    # node_grounding_enabled (default OFF, opt-in): per-node detect+correct pass
    # — score each groundable node's output against its upstream evidence and
    # CoVe-revise in place when it drifts, fixing it before it propagates
    # downstream. Adds ~1 verifier call per groundable node (CPU-bound cost), so
    # default-off. Both reuse grounding_min_score + cove_model_role.
    grounding_correct_enabled: bool = True
    node_grounding_enabled: bool = False
    # §17.576 — learning flywheel (opt-in, default OFF both directions). When a
    # job completes with grounding ≥ exemplar_min_grounding, its deliverable is
    # ingested into RAG tagged source_type="exemplar"; at DAG-plan time, similar
    # exemplars are retrieved + injected as few-shot "proven prior solutions".
    # Pollution guard: the grounding threshold + RAG's 3-tier dedup. Fail-soft.
    exemplar_ingest_enabled: bool = False
    exemplar_min_grounding: float = Field(default=0.85, ge=0.0, le=1.0)
    exemplar_retrieval_enabled: bool = False
    exemplar_retrieval_top_k: int = Field(default=2, ge=1, le=10)
    # §17.577 — adaptive escalation ladder (opt-in, default OFF). When a node
    # fails and is retried, escalate the model per retry rung: retry N uses
    # node_escalation_order[N-1] (clamped to the last rung). Implemented in
    # retry_failed_node (sets the node's assigned_model on reset), so it works
    # for BOTH the serial and parallel re-execution paths. node_escalation_to_assist:
    # when retries are exhausted, hand the job to Assist Mode (final human rung)
    # instead of just failing. Both fail-soft.
    node_escalation_enabled: bool = False
    node_escalation_order: list[str] = Field(default_factory=lambda: ["model_cloud_heavy"])
    node_escalation_to_assist: bool = False
    # §17.578 — best-of-N for DELIVERABLE nodes (opt-in, default OFF). Generate N
    # candidates concurrently, judge each by grounding (faithfulness vs the node's
    # upstream evidence), keep the best; the normal verifier then runs on the
    # winner. Deliverable nodes only (few) to bound the N× cost; non-CodeGen/Shell
    # with upstream evidence. Fail-soft (any error → single candidate).
    best_of_n_enabled: bool = False
    best_of_n_count: int = Field(default=2, ge=2, le=4)
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
    # §17.677 — when the operator raises a plan-affecting note mid-session
    # (constraint/decision/addition/preference), run an LLM impact analysis over
    # the still-pending nodes and surface a proposed plan fix for confirmation
    # ("surface-and-ask", not silent auto-rewrite). Disable to keep notes purely
    # feed-forward-as-text (§17.654 behavior) with no impact analysis.
    assist_note_replan_enabled: bool = True
    # §17.699 — proactive divergence re-plan. On context_only (the default
    # policy), the background verifier already detects when a submitted step's
    # evidence diverges from its plan, but it only sets an invisible
    # `assist_steps.divergence=TRUE` flag — the operator had to notice the plan
    # was wrong themselves and raise a note. When enabled, a MAJOR divergence
    # additionally runs the §17.677 note-impact analyzer over the pending nodes
    # and stages a surface-and-ask proposal (`metadata.pending_replan`,
    # note_kind='divergence'), which /assist next surfaces once. Reuses the
    # note/pivot confirm+apply path end to end. Disable to keep divergence
    # purely observational (the pre-§17.699 behavior).
    assist_divergence_replan_enabled: bool = True
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
    # §17.626 — natural-language assist turns. When a chat has an active assist
    # session, plain text is classified into an intent (advance / skip / submit /
    # fix / finalize / pause / question) so the operator drives the whole flow by
    # talking, not by /assist subcommands (the subcommands remain as aliases).
    # A single cheap tool_call per substantive message (obvious verbs like
    # "next"/"skip" are matched deterministically in the pipeline with no LLM).
    # §17.771 (Phase 0) — moved OFF the verifier (kimi) to model_general
    # (deepseek-v4-pro). kimi reliably false-negatives on assist semantics: the
    # §17.677 note-impact analyzer was already forced onto model_general for the
    # same reason, and this classifier's weakness is precisely what the ~8
    # compensating phrase gates in the pipeline exist to patch. model_general
    # does clean native tool-calling on this stack (proven by analyze_note_impact
    # / the §17.632 A/B). Reversible: set ASSIST_CLASSIFY_MODEL_ROLE=model_verifier.
    # Fail-soft: on any classify error the turn falls back to 'question' (the
    # pre-§17.626 guide behavior).
    assist_nl_turns_enabled: bool = True
    assist_classify_model_role: str = "model_general"
    # §17.771 (deferred, now done) — render-path decision suggestion validation.
    # On a DECISION step's first view, guarantee the walkthrough carries a
    # "## My suggestion" lean (parity with the now-decisive commit path); if the
    # model dropped it, generate just that block from the options it produced and
    # append it. Code default OFF so tests + fresh installs keep the legacy path
    # (and never fire the follow-up call); live-on via compose. Fail-soft.
    assist_decision_suggestion_enforce: bool = False
    # §17.771 — the unified assist decision (`assist_decide.decide_turn`): ONE
    # context-rich call that replaces the fragmented classifier + phrase-gates +
    # track + reroute. THIS server-side flag gates the /decide ENDPOINT (returns
    # 404 when off, §17.810). A SEPARATE pipeline valve of the same name (in
    # `pipelines/scaffold_router/valves.json`) gates whether the pipeline actually
    # DISPATCHES on the Decision — the two are independent despite sharing a name.
    # §17.812 — LIVE behavior is Phase 2 (dispatch on the Decision), with the
    # deterministic shell-error signal re-applied as a VETO post-filter in
    # `_dispatch_decision` and the cascade as the low-confidence/error fallback.
    # (The Phase-1 SHADOW mode — Decision logged for comparison, pipeline unchanged
    # — is historical.) Default OFF so tests + fresh installs keep the legacy path.
    assist_unified_decision_enabled: bool = False
    assist_decide_model_role: str = "model_general"
    # §17.771 (post-verify) — the Phase-1 SHADOW logger, now DECOUPLED from the
    # authority valve above. Once the unified decision is the live authority, the
    # shadow's data-gathering purpose is done: it just fires a redundant
    # model_general call on fall-through turns and writes a diagnostic
    # `[shadow §17.771]` note to the operator's friction log. Gate it on its OWN
    # valve (default OFF) so authority can be on without the shadow touching live
    # sessions; flip on only for a fresh shadow-comparison study.
    assist_shadow_decision_enabled: bool = False
    # §17.771 (Phase 0) — the SUBMIT-path divergence verifier (detect_divergence).
    # Was hardcoded to model_verifier (kimi), whose false-negatives fail SILENT
    # here (no divergence flag, no proposal → under-react). Same lesson as the
    # classifier above and §17.677. Reversible: ASSIST_DIVERGENCE_MODEL_ROLE=...
    assist_divergence_model_role: str = "model_general"
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
    # §17.731 — block a commit when the evidence shows the step's DELIVERABLE
    # isn't done yet (verdict 'incomplete'), distinct from a command 'failed'.
    # The live failure: "Install guest OS" was marked done from an ISO-download
    # paste (operator was still at the installer boot menu) — not a failure
    # signal, so the conservative verifier said 'unclear' and it committed,
    # marching the plan two steps past reality. 'incomplete' catches "you did
    # the setup but not the actual task". Conservative by design (the verifier
    # only returns it on affirmative not-done evidence; ambiguity stays
    # 'unclear' → commits). Code default off (preserves legacy + tests); live
    # via compose. The step stays claimable so the operator finishes it — or
    # `/assist skip` to override a false block.
    assist_block_on_incomplete_verify: bool = False
    # §17.490 — after a submit, extract the concrete values the operator
    # actually used (the IP/path/name they filled into a <PLACEHOLDER> the
    # walkthrough emitted) from their evidence and fold them into the session
    # environment, so later steps' walkthroughs are concrete instead of
    # re-emitting the same placeholder. Only fires when the step's cached
    # guidance actually contained placeholders (no LLM call otherwise);
    # fail-soft; only-add-new (never overwrites a value the operator set or a
    # previously-learned one).
    assist_learn_substitutions: bool = True
    # §17.709 — the session FACTS ledger. Substitution-learning only fires when a
    # step's guidance had <PLACEHOLDER> tokens, so an audit/inventory/gather step
    # (real system state, no placeholders) retained NOTHING — later decision steps
    # then fabricated assumptions ("Assumption: Fresh Proxmox VE server") despite
    # the audit. On each substantive submit we now distill durable FACTS about the
    # operator's actual system (installed software/versions, existing
    # users/VMs/pools/storage/network, whether it's a fresh vs existing system,
    # and inconclusive checks — e.g. a command that errored so state is UNKNOWN)
    # into metadata.environment.facts, rendered into EVERY later guidance +
    # decision context (compact, so it survives digest truncation). model_general
    # (a reasoning/extraction task, not verification); fail-soft. Cap keeps the
    # injected block bounded; oldest facts drop first.
    assist_capture_facts_enabled: bool = True
    assist_facts_max: int = Field(default=40, ge=1, le=200)
    # §17.710 — unified session memory. Master gate + per-stage sub-valves so the
    # refactor rolls out incrementally and each stage A/Bs against today's
    # behavior. Default OFF: `assist_unified_memory_enabled=False` keeps the
    # legacy scattered-channel behavior exactly. Stage A (capture) is inert
    # recording — safe even alone once the master gate is on. Stage B (inject)
    # and C (grounding, warn-only) are toggled independently. `umem_max_chars`
    # bounds the single injected memory block (Stage B). See db/migrations/057.
    assist_unified_memory_enabled: bool = False   # master gate for §17.710
    assist_umem_capture: bool = True              # Stage A — record raw turns
    assist_umem_inject: bool = False              # Stage B — consolidate + inject
    assist_umem_grounding: bool = False           # Stage C — pre-commit warn on contradiction
    assist_umem_max_chars: int = Field(default=4000, ge=500, le=20000)
    # §17.715 — unconditional per-message DERIVE. §17.710a made CAPTURE
    # unconditional, but the review-and-log step stayed trigger-gated (only
    # skip/question≥6w pivots, explicit notes, and submit-facts). A plan change
    # in a message routed to ask/fix/etc. was captured raw but never derived into
    # the notes/facts guidance injects. When on, EVERY operator message runs one
    # cheap model_general extraction (off the request path, dedup-safe) that logs
    # any plan-relevant note (decision/constraint/addition/preference) or durable
    # fact. Silent scribe — the interactive re-plan surface stays on the explicit
    # pivot path (§17.693). Default OFF (legacy behavior); flip live via env.
    assist_umem_derive: bool = False              # §17.715 — derive memory from every turn
    # §17.725 — fact SUPERSESSION. The ledger is append-only and `set_environment`
    # only dedups exact matches, so a new observation that CONTRADICTS an earlier
    # fact left both in the ledger (live: "P40 in group 13" AND "P40 in group 37")
    # and every later prompt grounded on the contradiction. When on, the two
    # distillers (§17.709 submit facts, §17.715 per-turn scribe) are shown the
    # known facts and may echo the ones the new evidence directly contradicts;
    # those verbatim matches are RETRACTED from the ledger as the new facts fold
    # in. The raw assist_turns transcript keeps everything (§17.710a), so a
    # retraction never destroys evidence. Default OFF; flip live via env.
    assist_umem_supersede: bool = False           # §17.725 — retract contradicted facts
    # §17.727 — ledger CONSOLIDATION. §17.725 removes direct contradictions, but
    # redundant same-truth facts still pile up (the live ledger stated the oasis
    # pool three ways), bloating every prompt and burning the §17.722 budget on
    # repetition. When on, a background pass (fired after a fold pushes the
    # ledger past `assist_facts_consolidate_min`, debounced) asks model_general
    # for MERGE GROUPS by index; application is deterministic and lossless by
    # construction — only facts explicitly in a valid ≥2-member group are
    # replaced (at the group's newest position), everything else is untouched,
    # and the raw assist_turns transcript keeps the originals. Default OFF.
    assist_umem_consolidate: bool = False         # §17.727 — merge redundant facts
    assist_facts_consolidate_min: int = Field(default=30, ge=5, le=200)
    # §17.499 — default walkthrough verbosity (terse | normal | detailed).
    # Per-session override via /assist verbose. terse = commands + one-line
    # whys (expert); detailed = explain why each step matters + what to watch
    # for (novice); normal = current behavior (no directive).
    assist_default_verbosity: str = "normal"
    # §17.500 — deep research: for /assist research + /assist fix, fetch & extract
    # the top-N SearXNG result pages (trafilatura, via the research-agent helper)
    # for real doc content instead of search snippets. The auto-guide pre-pass
    # stays snippet-fast (not deep) so walkthroughs don't slow down. 0 = snippet-
    # only everywhere.
    assist_research_fetch_top_n: int = Field(default=2, ge=0, le=5)
    # §17.650 — project-aware assist Q&A. Both the /assist research ("ask")
    # path and the step-guidance turn were job-BLIND: they answered an operator
    # question with a raw KB/web lookup that carried none of the project's own
    # state (refined brief, the research/plan the DAG already produced, the
    # captured environment). So "how do I connect the two computers" got a
    # generic answer that knew nothing about *these* machines. When enabled, a
    # compact project-wide digest of completed DAG-node outputs is threaded into
    # both paths so the engine relays what the project already established before
    # reaching for the open web. max_chars caps the digest (0 = disabled too).
    assist_job_context_enabled: bool = True
    assist_job_context_max_chars: int = Field(default=6000, ge=0, le=20000)
    # §17.687 — recent-conversation recall. The §17.650 digest recovers only
    # COMMITTED node output; notes recover only what the OPERATOR captured. So a
    # program the engine SUGGESTED a turn ago (a decision node's "## My
    # suggestion"), or any not-yet-committed back-and-forth, was forgotten on the
    # next turn — the guide/fix/research/classify paths never saw the live
    # dialogue. A follow-up like "define that one" / "yes, I'm interested" then
    # had no antecedent. When enabled, a windowed, truncated slice of the OWUI
    # conversation is threaded into those paths so references back resolve.
    # `turns` caps how many prior messages the pipeline forwards; `max_chars`
    # caps the rendered block on the orchestrator (0 on either = disabled).
    assist_conversation_context_enabled: bool = True
    assist_conversation_context_turns: int = Field(default=6, ge=0, le=30)
    assist_conversation_context_max_chars: int = Field(default=4000, ge=0, le=20000)
    # §17.738 — per-step running "progress recap". The 6-turn conversation
    # window above loses the thread over a long troubleshooting step (observed:
    # 37 turns on one step, engine re-suggesting resolved fixes and forgetting
    # which machine commands run on). A recap distilled from the FULL node-scoped
    # transcript (DB-backed) is injected into fix/guide/research AND surfaced to
    # the operator, so both stay oriented. Refreshed only when the step's turn
    # count grows by `assist_step_recap_every` since the last recap (cheap).
    # Code default off (legacy + tests); live via compose.
    assist_step_recap_enabled: bool = False
    assist_step_recap_every: int = Field(default=3, ge=1, le=20)
    assist_step_recap_min_turns: int = Field(default=4, ge=1, le=40)
    # §17.752 — the recap was node-scoped + transcript-only: it never read the
    # durable ledgers, so a constraint the operator stated on an EARLIER step (a
    # note) or a distilled system fact (§17.709) never reached CONSTRAINTS/CONTEXT
    # unless it was re-said in THIS node's transcript. When on, get_step_recap also
    # feeds the session's operator notes + observed facts into summarize_step_progress
    # so the recap grounds on the full record — while DONE/OPEN/NEXT stay
    # transcript-derived (a fact/constraint is not completed work). On by default:
    # it only fires when the recap already runs, and the ledgers are trusted data
    # already injected elsewhere. Flip off to restore the transcript-only recap.
    assist_recap_ledger_aware: bool = True
    # §17.752 — ground the note-impact / pivot analyzer (analyze_note_impact) in
    # the observed facts ledger, not just the brief: whether a new note actually
    # invalidates a pending step often depends on the operator's ACTUAL system
    # (e.g. "no TPM" only breaks a step if the plan assumed one). On by default.
    assist_note_impact_facts_aware: bool = True
    # §17.753 — the cross-step "living project recap" (§17.679): a distilled,
    # cached, EVOLVING whole-project state board (goal · done phases · in-progress ·
    # remaining · decisions · constraints · system facts). The per-step recap keeps
    # ONE step coherent and the job digest dumps raw done-node outputs; neither
    # gives step-N guidance/pivot the ARC. Refreshed only when the DONE-node count
    # grows by `assist_project_recap_every` (so ~one LLM call per completed step,
    # cached across the many turns within a step); starts once `min_nodes` are done.
    # Prepended to the job digest in the §17.751 funnel (so all 5 generation sites
    # get it) and threaded into the note/pivot analyzer. Code default off (extra
    # LLM path); live via compose.
    assist_project_recap_enabled: bool = False
    assist_project_recap_every: int = Field(default=1, ge=1, le=20)
    assist_project_recap_min_nodes: int = Field(default=1, ge=1, le=40)
    # §17.754 — the progress-TRACKING agent (operator-directed). The recap/facts
    # DESCRIBE state but never RECONCILE the session pointer with reality: an
    # operator finishes a step without submitting it, or starts a sub-task the plan
    # has no step for, and a help request gets answered against the stale step (the
    # "it just repeated itself" failure). On a substantive help/how-to turn, this
    # LLM agent reads where the operator ACTUALLY is against the DAG and returns an
    # action — on_step / advance / add_step. `add_step` (a real uncovered sub-task)
    # inserts a guided step (§17.736) and walks them through it instead of
    # repeating. Guardrails: valve-gated, confidence-thresholded, fail-soft to
    # on_step (never traps the turn or mutates the plan on a guess). Code default
    # off; live via compose.
    assist_progress_tracker_enabled: bool = False
    assist_tracker_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    # §17.755 — on a reset/rebuild note (§17.714), auto-RETRACT the facts that
    # describe the abandoned system (an LLM pass keeps durable host/network/storage/
    # new-build facts). §17.714 only demoted them at render time, so they lingered
    # and leaked. `max_frac` is a hard guardrail: never retract more than this
    # fraction of the ledger in one sweep (a mis-firing model can't wipe it). Code
    # default off; live via compose.
    assist_reset_facts_sweep_enabled: bool = False
    assist_reset_facts_sweep_max_frac: float = Field(default=0.9, ge=0.1, le=1.0)
    # §17.756 — ground-or-ask: a prompt directive on guide/fix so any
    # operator-specific value (username, IP, hostname, path, …) NOT in the confirmed
    # facts is emitted as a <PLACEHOLDER> and listed under a `## Confirm these
    # values` section — instead of hardcoding a stale guess lifted from the
    # transcript (the `ai-defruscio` username leak). Folded into generation (no
    # extra LLM call). Skipped for decision nodes. Code default off; live via compose.
    assist_ground_or_ask_enabled: bool = False
    # §17.757 — cross-component fact sharing. A decomposed umbrella project (e.g. a
    # homelab: Proxmox + GPU + media stack + game server) has components that share
    # ONE host / network / storage, but facts (§17.709) are session-scoped, so a
    # later component starts blind to what an earlier one learned (host NAT, the
    # bridge, the ZFS pool, hardware). When on, the §17.751 funnel folds the facts
    # observed on SIBLING components (same `parent_job_id`) into the environment the
    # generation grounds on — deduped and capped. Standalone (non-umbrella) jobs are
    # unaffected. Code default off; live via compose.
    assist_cross_component_facts_enabled: bool = False
    assist_cross_component_facts_cap: int = Field(default=40, ge=1, le=200)
    # §17.759 — share only DURABLE cross-cutting infrastructure facts (hardware /
    # host network / storage / access), not transient states ("nic DOWN") or
    # component-specific detail. An LLM classifier tags each session's durable
    # subset, CACHED in metadata.environment.durable_facts (refreshed when the fact
    # count changes) so there's no classifier call at generation time. On classifier
    # failure, falls back to sharing all facts (the §17.757 behavior). Flip off to
    # share all sibling facts. Default on when cross-component sharing is on.
    assist_cross_component_durable_only: bool = True
    # §17.761 — reconnect orientation: on /assist/start (a fresh chat picking a job
    # back up, or /assist <job>), attach a compact WHERE-YOU-ARE snapshot (title ·
    # progress · recently-done · current step · what's next + the cached project
    # recap) so the operator gets context instead of being dropped into a raw step.
    # Deterministic + reads the CACHED project recap (no model call on the start
    # path). Code default off; live via compose.
    assist_reconnect_orientation_enabled: bool = False
    # §17.758 — screen-state grounding: a directive on guide/fix so a walkthrough
    # for an INTERACTIVE surface (OS installer, TUI, BIOS/boot menu, noVNC console,
    # web wizard) whose current screen isn't confirmed OPENS by asking what's on
    # screen and makes the first action conditional — instead of assuming a screen
    # and sending keystrokes to the wrong place (the storage-screen assumption).
    # Folded into generation (no extra LLM call); skipped for decision nodes. Code
    # default off; live via compose.
    assist_screen_grounding_enabled: bool = False
    # §17.741 — surface the running recap to the OPERATOR as a "📍 Where we are"
    # panel above each walkthrough (goal / done / open / next), so a first-timer
    # can always see the engine holding the thread on a long problem-solving
    # step. The panel is a presentation of the §17.738 recap, so enabling it
    # also forces the recap to be computed (see get_step_recap) even if
    # assist_step_recap_enabled is off. Code default off; live via compose.
    assist_status_panel_enabled: bool = False
    # §17.741 — lead each guide/fix turn with a "👉 Do this next" section: the
    # single most-immediate action (one command / one concrete step), before the
    # fuller walkthrough — so the operator's attention lands on what to do NOW.
    # Prompt-level (a leading section the model emits); skipped for decision
    # nodes (they suggest a choice, not an action). Code default off; live via
    # compose.
    assist_next_callout_enabled: bool = False
    # §17.742 — problem-solving discipline for TANGLED, multi-attempt steps. Live
    # evidence (P40/T14: 48 assistant turns on one step, 4 approaches tried+failed)
    # showed the engine THRASHING — re-proposing ruled-out approaches and asking
    # for output the operator couldn't give (no copy-paste in noVNC, guest agent
    # down). When on, fix/guide/ask carry a discipline framing (honor confirmed
    # CONSTRAINTS, stop cycling once approaches have failed and commit to ONE
    # path, match the operator's real capability) and the recap distills a
    # CONSTRAINTS section. Code default off; live via compose.
    assist_problem_solving_enabled: bool = False
    # §17.689 — multi-turn decision deliberation. A decision node whose
    # deliverable is a CONCRETE artifact (a VLAN table, a partition layout, a
    # config set) used to commit on the operator's FIRST partial answer ("3
    # vlans"), leaving downstream steps to invent the concrete values. When on,
    # a decision step's submit is intercepted: the engine assembles the concrete
    # artifact ACROSS turns (propose → the operator confirms/adjusts) and only
    # commits the full, confirmed artifact — so T2 "Define VLAN plan" actually
    # yields the table T12/T17 build from. A simple binary decision still
    # resolves in one turn (the LLM marks it resolved immediately). Fail-soft:
    # any deliberation error falls back to the plain single-turn commit.
    assist_decision_deliberation_enabled: bool = True
    # §17.693 — semantic pivot detection. Deterministic phrase gates (§17.679/
    # 691/692) miss pivots phrased as references to the operator's ACTUAL
    # situation ("I already have Proxmox installed, we only need to remove the
    # old containers") — the classifier then mis-routes them to skip/question and
    # the plan marches on with now-irrelevant steps. When on, a substantive turn
    # the classifier read as skip/question is checked against the pending plan by
    # the §17.677 impact analyzer; if it invalidates steps, the engine surfaces a
    # re-plan instead. Fail-soft + dry-run (no side effects when nothing's hit).
    assist_pivot_detect_enabled: bool = True
    # §17.747 — on a detected PIVOT, also let the impact analyzer propose
    # REOPENING already-done nodes whose result the pivot destroyed (e.g. "delete
    # VM 100 and recreate" undoes the Ubuntu install + network config on the old
    # VM). Surface-and-ask: the operator confirms via /replan/apply before any
    # finished work is reset. Reopening resets the node to pending so its stale
    # "done" output stops leading the prompt as MANDATORY upstream context.
    # Code-default OFF (fresh installs / tests keep the pending-only behavior);
    # live via compose ASSIST_PIVOT_REOPEN_ENABLED=true.
    assist_pivot_reopen_enabled: bool = False
    # §17.763 — the §17.693 fuzzy-reroute path (detect_reroute) runs the impact
    # analyzer over a message the turn classifier only weakly placed
    # (question/skip). With this on, that path analyzes CONSERVATIVELY: it flags a
    # re-plan only when the message states a concrete situation-fact that
    # contradicts a specific pending step — a plain request for help / a how-to /
    # confusion is NOT treated as a plan change. The liberal err-toward-flagging
    # bias stays on the EXPLICIT-note path (assess_note_impact). Fixes "asked for
    # help, it reverted to DAG planning". Flip off to restore the old shared bias.
    assist_reroute_strict: bool = True
    # §17.492 — deterministic scan of generated walkthroughs / fixes for
    # high-confidence destructive commands (rm -rf, dd, mkfs, DROP TABLE, force
    # push, …); matches are surfaced as a prominent "review before running"
    # banner ahead of the steps. No LLM, no blocking — the operator is the
    # executor; this informs. Default on.
    assist_destructive_scan: bool = True
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
    # §17.776 — stream per-node LLM content deltas to the SSE consumer as
    # `node_token` events, so the operator sees output as it generates instead
    # of waiting for the full `node_done`. Default OFF: the streaming path uses
    # model_router.stream_chat, which (unlike chat) does NOT _record_call token
    # usage mid-stream — so a fully-streamed node isn't cost-tracked. The
    # empty-guard fallback (§17.465) still runs through the non-stream chat and
    # IS recorded. Opt in per-host when live-token UX matters more than exact
    # per-node cost attribution. Only the serial execute path streams tokens
    # today; the parallel-frontier path is unaffected when this is on (its
    # per-node deltas are a deferred follow-up).
    node_token_streaming_enabled: bool = Field(default=False)
    # §17.811 — progress + ETA signal. When on, long-running subsystems (DAG
    # exec, research, RAG ingest, assist, decompose, sim) emit a `progress` SSE
    # frame carrying an elapsed-rate ETA (§17.812, concurrency-correct) and a
    # deterministic one-line summary. The DAG path computes the read-path snapshot
    # ON DEMAND from dag_nodes timestamps (no metadata write — parallel-frontier
    # race); the serial subsystems persist it to their state row. Deterministic
    # and cheap — no model call in the hot loop.
    progress_eta_enabled: bool = Field(default=True)
    # Throttle: at most one live `progress` emit (+ state persist where the
    # subsystem persists) per this many seconds — the terminal snapshot and the
    # first tick always land. Bounds SSE chatter + DB writes on a fast burst.
    progress_emit_min_interval_seconds: float = Field(default=5.0, ge=0.0, le=120.0)
    # §17.812 — LEGACY: smoothing for the per-unit EWMA that no longer drives the
    # ETA (now elapsed-rate). Still accepted by ProgressTracker(alpha=) and folded
    # by tick(); retained to avoid a signature/knob churn. No effect on the ETA.
    progress_ewma_alpha: float = Field(default=0.3, ge=0.01, le=1.0)
    # Opt-in LLM-narrated rolling summary (the `📝` prose line). OFF by default:
    # this host is CPU-only and it adds a model_general call per emit. The
    # deterministic summary ships regardless; this only ADDS a prose line.
    progress_summary_llm_enabled: bool = Field(default=False)
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
    # §17.517 — general node grounding (_fetch_rag_context) fans out across ALL
    # domain partitions instead of scoping to the job's single domain. The
    # `domain` partition is a heuristic storage bucket, not a relevance boundary:
    # `/research` ingests under `_detect_domain(topic)` while a job carries its
    # own ideation-assigned domain, so scoping silently dropped relevant research
    # binned into a different partition. The cosine floor (rag_cosine_floor) +
    # reranker already filter cross-domain noise, so fan-out is strictly more
    # recall-complete. Set False to restore the old job-domain-scoped behavior.
    execution_grounding_cross_domain: bool = True
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
    # §17.665 — bumped 1024 → 3072: the validator role (model_general →
    # qwen3.5:397b-cloud) is a *thinking* model that spends tokens on a <think>
    # block BEFORE the JSON; 1024 let it burn the whole budget reasoning and
    # emit EMPTY content (the §17.463 lesson, for the validator this time), which
    # showed up as dag_validator_json_parse_failed raw='' → silent fail-open.
    # §17.686 — bumped 3072 → 6144 (bound 8192 → 16384): validating a much larger
    # DAG (up to dag_max_nodes=40) produces more per-task feedback, and the
    # thinking-model <think> block eats budget first.
    dag_validator_max_tokens: int = Field(default=6144, ge=256, le=16384)
    # §17.665 — retry-on-empty for the validator: re-draw up to N times when a
    # SUCCESSFUL response is empty/unparseable (thinking-model empty content). A
    # hard failure (success=False) is not retried. 0 disables the extra draws.
    dag_validator_empty_redraws: int = Field(default=2, ge=0, le=5)
    # §17.686 — goal-completion DAG sizing. Raised the per-job node ceiling from
    # the old hard-coded 10 (§17.685) so a complex build is planned as ALL the
    # nodes the goal needs rather than truncated/consolidated. dag_generation_max_
    # tokens MUST scale with it: with think=False the whole budget is the JSON
    # answer, so a 40-node DAG (~15-20k tokens) needs generous headroom or it
    # truncates mid-JSON (done=length). min_count stays 3.
    dag_max_nodes: int = Field(default=40, ge=3, le=120)
    dag_generation_max_tokens: int = Field(default=32768, ge=2048, le=65536)
    # §17.476 (Phase 2) — dependency-completeness / dead-end detection. A
    # substantive node whose output neither feeds nor is fed by any
    # is_deliverable node is an orphan branch (the §17.471-474 defect). When
    # enabled, the generator flags orphans in the validator retry loop so the
    # model re-decomposes; any survivors are auto-linked to the primary
    # deliverable as a deterministic last resort. Disable to skip the check.
    dag_dead_end_check_enabled: bool = True
    # §17.671 — wire a DECISION node ("Decide X") to the step that APPLIES it
    # ("Configure X"), matched on distinctive shared subject tokens, when the
    # generator made the decision but never consumed it. Best-effort/cycle-safe;
    # a miss just leaves it for convergence (§17.670). Runs BEFORE convergence.
    dag_wire_decisions_enabled: bool = True
    # §17.672 — decomposition-completeness: flag a DECISION with NO implementer
    # step in the plan (decided something, never added the step that carries it
    # out). Feeds the validator retry loop so the generator ADDS the missing step;
    # any survivor is surfaced as an `unimplemented_decisions` warning.
    dag_decision_impl_check_enabled: bool = True
    # §17.670 — converge multiple terminal leaves (dangling decisions + parallel
    # config steps that never join) into a SINGLE final sink, so the plan flows to
    # one deliverable instead of several loose ends. Deterministic, cycle-safe.
    dag_converge_terminals_enabled: bool = True
    # §17.696 — deterministically BREAK dependency cycles in a generated DAG
    # (remove the minimal back-edges) instead of failing the whole component job
    # with 0 nodes. A cyclic LLM draw ("VLAN Segmentation & AdGuard Home DNS"
    # failed this way) is now repaired like the other §17.668-670 well-formedness
    # passes. Off ⇒ pre-§17.696 behaviour (raise → job fails on any cycle).
    dag_break_cycles_enabled: bool = True
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
    # §17.568 — parallel-frontier execution WITHIN one job (independent
    # dep-satisfied nodes run concurrently). §17.571 — PROMOTED to default ON
    # after the prototype proved out: unit + integration (atomic claim) + live
    # diamond (pipeline_complete correct; 2.6× wall-clock when the cloud serves
    # the frontier concurrently). max_inflight DEFAULT LOWERED 4 → 2 to match
    # this host's budget: Ollama NUM_PARALLEL=4 split across
    # execution_global_concurrency=2 concurrent jobs = 2 in-flight nodes/job is
    # the non-contending sweet spot (4 would over-subscribe under 2 live jobs).
    # max_inflight caps concurrent nodes per job (distinct from
    # execution_global_concurrency, which caps concurrent jobs). Operators on
    # stronger inference hardware can raise it. R6: under heavy contention a node
    # could approach node_orphan_threshold_minutes (30) — raise that if enabling
    # a high max_inflight on slow hardware (this host's STALE_THRESHOLD=1560).
    parallel_execution_enabled: bool = Field(default=True)
    parallel_execution_max_inflight: int = Field(default=2, ge=1, le=16)
    # §17.809 — per-node execution-speed levers (default ON = unchanged
    # behaviour; the quick profile flips them OFF). Both are pure overhead on the
    # serial critical path: the CPU cross-encoder reranker adds ~21 s/node on
    # this host (RRF-only grounding stays available when off), and the per-node
    # LLM prompt-optimize pass ~6 s/node. Read live at execution time so a
    # runtime profile toggle takes effect on the next node.
    execution_rerank_enabled: bool = Field(default=True)
    execution_optimize_enabled: bool = Field(default=True)
    # §17.774 — automatic crash-resume of orphaned mid-execution jobs.
    # After a process crash the lifespan sweep resets the interrupted node
    # 'running'->'pending' but the parent job stays 'running' and nothing
    # re-launches it, so a 45-min run stalls until the 26h reaper fails it and
    # the operator hand-fires /exec retry. With this ON, a startup pass
    # (app/modules/execution_resume.py) re-drives execute_all_nodes for every
    # such job, resuming at the reset node and reusing all already-'done'
    # outputs. Idempotent (the executor skips done nodes) and bounded by the
    # crash-loop guard below, so default ON is safe; set false to disable the
    # auto-resume and fall back to the manual /exec retry flow.
    execution_resume_on_startup_enabled: bool = Field(default=True)
    # Crash-loop guard: a job whose restart makes ZERO new 'done' nodes has its
    # resume_attempts counter incremented; once it exceeds this cap the job is
    # marked 'failed' (error_summary 'crash_resume_budget_exhausted') instead of
    # restart-storming. A restart that DOES make progress resets the counter, so
    # this only trips on a genuinely poisonous node (e.g. one that OOM-kills the
    # process). Default 3 tolerates transient crashes while still surfacing a
    # stuck node within a few boots.
    execution_max_resume_attempts: int = Field(default=3, ge=1, le=10)
    # §17.777 — hard per-job cost/token budgets. Sprint J.3 already tallies
    # every LLM call (tokens + USD) into llm_call_logs tagged by job; this
    # valve turns that tally into an ENFORCED cap. When ON, execute_next_node
    # checks the running spend before each node and hard-stops a job that has
    # exceeded EITHER its token or USD budget (job -> 'failed',
    # error_summary 'cost_budget_exhausted'). Default OFF: no behavior change
    # until an operator opts in. Per-job overrides live on jobs.token_budget /
    # jobs.cost_budget_usd (NULL = inherit the two defaults below).
    cost_budget_enforcement_enabled: bool = Field(default=False)
    # Default token cap applied to every job when its jobs.token_budget is
    # NULL. Total tokens = prompt + completion, summed across all LLM calls.
    # 0 = unlimited (the default) — a token cap only bites once set > 0 (here
    # or per-job). Meaningful even on the all-Ollama deployment where USD is $0
    # because model_costs has no rows for the local/:cloud tags.
    cost_budget_default_max_tokens: int = Field(default=0, ge=0)
    # Default USD cap applied when jobs.cost_budget_usd is NULL. 0 = unlimited.
    # Only bites when the job's models are priced in model_costs (OpenAI /
    # Anthropic providers, or seeded :cloud tags); otherwise cost stays $0 and
    # the token cap is the effective lever.
    cost_budget_default_max_usd: float = Field(default=0.0, ge=0.0)
    # §17.786 — full request/response trace capture. Sprint J.3's llm_call_logs
    # records only the METRICS of each LLM call (tokens/latency/cost); this valve
    # additionally captures the CONTENT — prompt or serialized messages, system,
    # sampling params, response text, tool calls, error — into the `llm_traces`
    # table (JOINs 1:1 to llm_call_logs on job_id/node_id). Default OFF because
    # storing full prompts/responses has storage + PII implications; flip on to
    # debug a run or build a replay corpus. Fire-and-forget: a trace-write
    # failure never breaks the LLM call path (mirrors record_llm_call).
    trace_capture_enabled: bool = Field(default=False)
    # Per-field truncation cap for captured content (system/request/response).
    # Bounds a single trace row so a runaway prompt/response can't bloat the
    # table; the truncated text is suffixed with a "…[+N chars]" marker.
    trace_capture_max_chars: int = Field(default=8000, ge=256, le=1_000_000)
    # §17.442 — bound concurrent ideation requests (/ideas + /ideate). Unlike
    # execution, ideation had NO cap: the §17.441 stress test fired 6 concurrent
    # /ideate and all 6 hit the cloud at once (latency 33→81 s). The cap queues
    # bursts instead — acquired at the router layer so jobs aren't even created
    # until a slot frees. Default 4 (ideation is cloud-bound, not CPU-bound like
    # execution, so a higher cap than execution's 2 is fine).
    ideation_global_concurrency: int = Field(default=4, ge=1, le=32)
    # §17.531 — task-decomposition controls.
    # decompose_enabled: server-side kill switch. When false, POST /decompose
    #   415-rejects regardless of the pipeline's decompose_on_go valve (operator
    #   override that the chat surface can't bypass).
    # decompose_max_inflight_components: global ceiling on non-terminal component
    #   jobs. /decompose rejects (429) if creating its children would exceed it —
    #   bounds the total autonomous fan-out (and cloud cost) across ALL umbrellas,
    #   not just within one (MAX_COMPONENTS bounds a single umbrella).
    # decompose_component_stale_minutes: a component child stuck in an early phase
    #   (refining/awaiting_confirmation/researching/planning) past this is reaped
    #   to failed — recovers children stranded by a process restart far sooner
    #   than the generic 26h sweep, so umbrellas don't hang.
    decompose_enabled: bool = Field(default=True)
    decompose_max_inflight_components: int = Field(default=20, ge=1, le=500)
    decompose_component_stale_minutes: int = Field(default=180, ge=10, le=43200)
    # §17.574 — cap how many component pipelines EXECUTE concurrently within a
    # decomposition. They already spawn all-at-once and each child's DAG now runs
    # node-parallel (§17.571), so an N-component umbrella could otherwise stack
    # N×inflight inference calls on the host. ON by default (a safety bound on
    # existing unbounded fan-out); the rest queue. Distinct from
    # decompose_max_inflight_components (which throttles NEW decompositions).
    decompose_component_max_concurrent: int = Field(default=3, ge=1, le=20)
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

    # §17.772 — Model Context Protocol (MCP) integration. Two independent,
    # default-OFF gates (mirrors shell_tool_enabled: flipping a flag without
    # the backend wired fails loudly rather than silently degrading).
    #
    # CONSUMER side — when True, DAG nodes tagged tool='MCP' connect to a
    # registered external MCP server and invoke one of its tools during
    # execution (execute_next_node). When False, an MCP node is classified
    # non-executable (parked by the hands-on-assist gate), never fabricated.
    mcp_tool_enabled: bool = Field(default=False)
    # PRODUCER side — when True, the orchestrator exposes its own capabilities
    # as MCP tools over a streamable-HTTP transport mounted at /mcp (X-API-Key
    # gated). The `python -m app.mcp_server` stdio entrypoint is always
    # available regardless of this flag (separate process, exec-gated).
    mcp_server_enabled: bool = Field(default=False)
    # Seed registry of consumable MCP servers, merged UNDER the mcp_servers DB
    # table (a DB row overrides a config entry with the same name). JSON array,
    # e.g. [{"name":"github","transport":"streamable_http",
    #        "endpoint":"http://host:9000/mcp","headers":{"Authorization":"..."},
    #        "enabled":true}] or a stdio server
    #       {"name":"fs","transport":"stdio","command":"npx","args":["-y","..."]}.
    mcp_servers_config: str = Field(default="[]")
    # Per-call ceiling (seconds) for an outbound MCP tool invocation and for
    # tool-list introspection — bounds a hung external server.
    mcp_call_timeout: float = Field(default=60.0, ge=1.0, le=600.0)
    # Idle TTL (seconds) for a cached client session before it is torn down.
    mcp_session_ttl: float = Field(default=300.0, ge=10.0, le=3600.0)

    # §17.788 — native OpenAI-compatible surface. When True, the orchestrator
    # mounts an OpenAI chat-protocol sub-app at /v1 (POST /v1/chat/completions +
    # GET /v1/models), so any OpenAI client (OWUI as an OpenAI connection, the
    # /ui SPA chat, external SDKs) drives the engine directly and the OWUI
    # pipeline adapter becomes optional. Mounted as a sub-app so it bypasses the
    # global X-API-Key dependency and carries its own Bearer-or-X-API-Key guard
    # (require_openai_key). Default off — the pipeline path is unchanged while
    # off. Phase 0 is a passthrough to model_general; triage/NL routing land in
    # later phases (see docs/native_openai_surface_plan.md).
    native_openai_enabled: bool = Field(default=False)
    # Advertised model id on GET /v1/models and the default routed persona.
    native_openai_model_id: str = Field(default="scaffold-engine")

    # §17.624 — hands-on assist gate. When True (default), the autonomous
    # executor inspects a freshly-generated DAG before running it: if the
    # majority of nodes are non-autonomously-executable (Shell steps while
    # shell_tool_enabled is False, or human steps), it PARKS the job in
    # 'awaiting_assist' with the plan (nodes left 'pending') instead of
    # fabricating runbook "done" output and rolling up to a misleading
    # 'completed'. The operator then drives real execution via /assist.
    # Set False to restore the old behavior (autonomous runbook generation +
    # the §17.506 PLAN-NOT-EXECUTED banner).
    hands_on_assist_gate_enabled: bool = Field(default=True)
    # Fraction of a DAG's nodes that must be non-executable for the gate to
    # fire. 0.5 → strict majority hands-on parks the job; a mostly-LLM DAG with
    # a stray Shell step still runs autonomously.
    hands_on_assist_gate_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

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

    # §17.803 — role→model learning: a periodic golden re-A/B that STAGES swap
    # proposals (model_role_proposals) for human review as confirm cards. It
    # runs run_model_ab_task per role (real LLM calls), so it is default OFF and
    # scoped to the three roles with a golden task (coder/verifier/extract).
    # Nothing auto-swaps — accepting a proposal is what applies the override.
    model_role_learning_enabled: bool = False
    # Weekly by default (the goldens don't move often and each cycle costs LLM
    # calls). Range floor is one hour so a misconfig can't hammer the models.
    model_role_learning_interval_seconds: int = Field(
        default=7 * 86400, ge=3600, le=90 * 86400
    )
    # Repeats per (model, golden) trial — averages out per-call variance.
    model_role_learning_repeat: int = Field(default=3, ge=1, le=10)
    # Per-role EXTRA candidate model tags to A/B against the incumbent. A role
    # with no candidates is skipped (the incumbent alone can't be compared).
    # JSON-parseable env:
    #   MODEL_ROLE_LEARNING_CANDIDATES='{"model_coder":["kimi-k2.7-code:cloud","qwen3.5:397b-cloud"]}'
    model_role_learning_candidates: dict[str, list[str]] = Field(default_factory=dict)

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
    # out Redis. 0 disables the check entirely. §17.620 (audit #32) — the
    # count is now an O(1) DBSIZE against the fetch cache's own logical Redis
    # DB (fetch_cache_redis_db) rather than a SCAN over the shared keyspace.
    fetch_cache_max_keys: int = Field(default=50_000, ge=0, le=10_000_000)
    fetch_cache_count_interval_s: int = Field(default=30, ge=5, le=3600)
    # §17.620 (audit #32) — isolate fetch bodies in their own Redis logical DB
    # so the cardinality count is an exact, O(1) DBSIZE instead of a SCAN over
    # the shared 2GB allkeys-lru keyspace (dominated by millions of embedv3:*
    # keys). db0 holds the embedding/verifier/rag caches; db1 is free. Eviction
    # is still instance-wide under allkeys-lru, so this only isolates the
    # keyspace for counting, not the 2GB memory budget.
    fetch_cache_redis_db: int = Field(default=1, ge=0, le=15)

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
