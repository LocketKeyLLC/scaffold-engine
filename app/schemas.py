"""Scaffold Engine — Pydantic schemas for all 8 tables.

Each table has three schema variants:
  - Base:     shared fields (used for creation input)
  - Create:   alias for Base (explicit intent)
  - Read:     includes id, timestamps, and DB-generated fields

Step 6 of 23-step build plan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

JobStatus = Literal[
    "pending", "refining", "awaiting_confirmation", "researching", "planning", "executing",
    "running", "completed", "failed", "cancelled", "blocked",
    "assisted_executing", "assisted_running", "assisted_paused",
]

# Runtime-iterable mirror of JobStatus, derived directly from the Literal
# so the two cannot drift. Used by /jobs filter validation and any future
# status-aware code paths (cleanup reaper, etc.). Adding a new status to
# JobStatus automatically extends this tuple.
JOB_STATUSES: tuple[str, ...] = get_args(JobStatus)

# Statuses the cleanup reaper must NOT touch on its normal cadence.
# Assist sessions are user-driven and may legitimately stay open for
# days; the reaper consults `assist_sessions.last_activity_at` separately
# (see app/modules/cleanup.py:_REAP_ABANDONED_ASSIST_SQL).
ASSIST_PROTECTED_STATUSES: tuple[str, ...] = (
    "assisted_executing", "assisted_running", "assisted_paused",
)

# Whitelist of values the research_sessions.status column accepts. Mirror
# of the values written by app.modules.research_state and the pause flow
# from migration 013. Used by /research/sessions list filtering so an
# arbitrary status string can't ILIKE-match unrelated rows.
RESEARCH_SESSION_STATUSES: tuple[str, ...] = (
    "pending", "running", "paused_awaiting_reply",
    "completed", "failed", "cancelled",
)

NodeStatus = Literal["pending", "running", "done", "failed", "skipped"]

NodeType = Literal["task", "decision", "parallel_group", "checkpoint"]

LogLevel = Literal["debug", "info", "warning", "error", "critical"]

ErrorType = Literal[
    "transient", "timeout", "validation", "unrecoverable",
]

RecoveryAction = Literal["retry", "model_swap", "dag_replan", "manual", "none"]

ArtifactType = Literal[
    "dag", "prompt", "toon_file", "plan",
    "code", "report", "mermaid", "other",
]

RequestType = Literal["generate", "embed", "rerank", "classify"]

Severity = Literal["critical", "high", "medium", "low"]

BlockerCategory = Literal[
    "infrastructure", "model", "pipeline",
    "ui", "data", "performance", "other",
]

BlockerStatus = Literal["open", "in_progress", "resolved", "wont_fix"]
ResearchDepth = Literal["shallow", "medium", "deep"]


# ---------------------------------------------------------------------------
# 1. Jobs
# ---------------------------------------------------------------------------

class JobBase(BaseModel):
    title: str
    description: str | None = None
    input_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobCreate(JobBase):
    pass


class JobUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: JobStatus | None = None
    refined_brief: dict[str, Any] | None = None
    error_summary: str | None = None
    metadata: dict[str, Any] | None = None


class JobRead(JobBase):
    id: UUID
    status: JobStatus
    refined_brief: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error_summary: str | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 2. DAG Nodes
# ---------------------------------------------------------------------------

class DagNodeBase(BaseModel):
    node_key: str
    title: str
    description: str | None = None
    node_type: NodeType = "task"
    depends_on: list[str] = Field(default_factory=list)
    assigned_model: str | None = None
    prompt_template: str | None = None
    max_retries: int = 3
    parallel_group: int | None = None
    execution_order: int | None = None
    tool: str | None = None
    domain: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    is_output_node: bool = False


class DagNodeCreate(DagNodeBase):
    job_id: UUID


class DagNodeUpdate(BaseModel):
    status: NodeStatus | None = None
    assigned_model: str | None = None
    optimized_prompt: str | None = None
    output_text: str | None = None
    output_artifact_id: UUID | None = None
    retry_count: int | None = None


class DagNodeRead(DagNodeBase):
    id: UUID
    job_id: UUID
    status: NodeStatus
    optimized_prompt: str | None = None
    output_text: str | None = None
    output_artifact_id: UUID | None = None
    retry_count: int = 0
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 3. Execution Logs
# ---------------------------------------------------------------------------

class ExecutionLogBase(BaseModel):
    job_id: UUID
    node_id: UUID | None = None
    log_level: LogLevel = "info"
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ExecutionLogCreate(ExecutionLogBase):
    pass


class ExecutionLogRead(ExecutionLogBase):
    id: UUID
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 4. Error Logs
# ---------------------------------------------------------------------------

class ErrorLogBase(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    job_id: UUID | None = None
    node_id: UUID | None = None
    error_type: ErrorType
    error_message: str
    stack_trace: str | None = None
    model_used: str | None = None
    recovery_action: RecoveryAction | None = None
    recovery_model: str | None = None


class ErrorLogCreate(ErrorLogBase):
    pass


class ErrorLogUpdate(BaseModel):
    retry_count: int | None = None
    recovery_action: RecoveryAction | None = None
    recovery_model: str | None = None
    resolved: bool | None = None
    resolution: str | None = None


class ErrorLogRead(ErrorLogBase):
    id: UUID
    retry_count: int = 0
    resolved: bool = False
    resolution: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())


# ---------------------------------------------------------------------------
# 5. Artifacts
# ---------------------------------------------------------------------------

class ArtifactBase(BaseModel):
    job_id: UUID
    node_id: UUID | None = None
    artifact_type: ArtifactType
    title: str
    content: str | None = None
    file_path: str | None = None
    mime_type: str = "text/plain"
    size_bytes: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactCreate(ArtifactBase):
    pass


class ArtifactRead(ArtifactBase):
    id: UUID
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 6. Performance Logs
# ---------------------------------------------------------------------------

class PerfLogBase(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    job_id: UUID | None = None
    node_id: UUID | None = None
    model: str
    endpoint: str
    request_type: RequestType = "generate"
    ttft_ms: int | None = None
    total_duration_ms: int
    tokens_prompt: int | None = None
    tokens_completion: int | None = None
    tokens_per_sec: float | None = None
    success: bool = True
    error_message: str | None = None


class PerfLogCreate(PerfLogBase):
    pass


class PerfLogRead(PerfLogBase):
    id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())


# ---------------------------------------------------------------------------
# 7. Benchmark Results
# ---------------------------------------------------------------------------

class BenchmarkBase(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model: str
    domain: str
    benchmark_name: str
    score: float
    max_score: float = 1.0
    sample_count: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class BenchmarkCreate(BenchmarkBase):
    pass


class BenchmarkRead(BenchmarkBase):
    id: UUID
    run_at: datetime

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())


# ---------------------------------------------------------------------------
# 8. Blockers
# ---------------------------------------------------------------------------

class BlockerBase(BaseModel):
    title: str
    description: str | None = None
    severity: Severity = "medium"
    category: BlockerCategory | None = None


class BlockerCreate(BlockerBase):
    pass


class BlockerUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    severity: Severity | None = None
    category: BlockerCategory | None = None
    status: BlockerStatus | None = None
    resolution: str | None = None


class BlockerRead(BlockerBase):
    id: UUID
    status: BlockerStatus
    resolution: str | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None

    model_config = {"from_attributes": True}

# ---------------------------------------------------------------------------
# Step 14: Prompt Optimizer
# ---------------------------------------------------------------------------

class PromptOptimizeInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    prompt: str
    model_optimizer: str | None = None
    model_verifier: str | None = None
    skip_verify: bool = False
    model_overrides: dict | None = None

class PromptOptimizeResult(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    original_prompt: str
    optimized_prompt: str
    pre_cleaned: str
    token_count_before: int
    token_count_after: int
    token_reduction_pct: float
    clarity_score: float
    intent_preserved: bool
    issues_found: list[str]
    issues_resolved: list[str]
    model_used: str
    verifier_used: str

# ---------------------------------------------------------------------------
# Step 15: Execution Agent
# ---------------------------------------------------------------------------

class ExecuteNextInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    job_id: str
    skip_optimize: bool = False
    skip_verify: bool = False
    model_overrides: dict | None = None

class ResumeJobInput(BaseModel):
    """Body for POST /jobs/{job_id}/resume. ``job_id`` comes from the path."""
    model_config = ConfigDict(protected_namespaces=())
    skip_optimize: bool = False
    skip_verify: bool = False
    model_overrides: dict | None = None

class SkipNodeInput(BaseModel):
    job_id: str
    node_key: str

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, v: str) -> str:
        try:
            UUID(v)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"job_id must be a valid UUID: {exc}")
        return v

    @field_validator("node_key")
    @classmethod
    def _validate_node_key(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("node_key must be non-empty")
        return v.strip()

class ExecutionResult(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    status: str
    job_id: str | None = None
    node_key: str | None = None
    title: str | None = None
    output: str | None = None
    verified: bool | None = None
    verification_reason: str | None = None
    confidence: float | None = None
    model_used: str | None = None
    prompt_used: str | None = None
    awaiting_approval: bool | None = None
    message: str | None = None
    tool: str | None = None
    error: str | None = None

# ---------------------------------------------------------------------------
# Research Agent
# ---------------------------------------------------------------------------

class ResearchInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    topic: str
    depth: ResearchDepth = "medium"
    domain: str | None = None
    model_overrides: dict | None = None


class ResearchReplyInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    session_id: str
    reply: str
    model_overrides: dict | None = None
# ---------------- Scheduled research jobs ----------------

class ScheduleCreate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    topic: str
    cron_expression: str  # e.g. "0 9 * * 1" = Mondays at 09:00 in `timezone`
    depth: ResearchDepth = "medium"
    timezone: str = "UTC"  # IANA tz name, e.g. "America/New_York"
    model_overrides: dict | None = None


class ScheduleResponse(BaseModel):
    id: int
    topic: str
    depth: str
    cron_expression: str
    timezone: str = "UTC"
    enabled: bool
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_job_id: str | None = None
    next_run_at: datetime | None = None
    run_count: int
    failure_count: int
    created_at: datetime

# ---------------------------------------------------------------------------
# Phase 2 (fix-list #13, #14): endpoint body schemas moved from main.py
#   + two new schemas for raw-request → Pydantic conversion.
# ---------------------------------------------------------------------------


class IdeaInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    idea: str
    domain: str | None = None
    model: str | None = None
    model_overrides: dict | None = None


class ConfirmInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    job_id: str
    feedback: str | None = None
    push_to_github: bool = False
    model_overrides: dict | None = None


class DagInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    job_id: str
    model: str | None = None
    model_overrides: dict | None = None


class RagInput(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1, le=100)
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    skip_rerank: bool = False
    include_history: bool = False
    domain: str | None = None
    # §17.118 — per-intent embedder instruction. Validated against the
    # EMBED_QUERY_TEMPLATES keys at the endpoint layer; pydantic Literal
    # gives a clean 422 response shape for unknown intents.
    query_intent: Literal["general", "code", "qa", "paper"] = "general"


class GtInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    topic: str
    queries: list[str] | None = None
    push_to_github: bool = False
    target_file: str | None = None
    model: str | None = None
    github_owner: str | None = None
    github_repo: str | None = None


class GtSearchInput(BaseModel):
    domain: str | None = None
    query: str
    top_k: int = 10
    include_history: bool = False


class PromptUpdateInput(BaseModel):
    """Body for POST /prompts/{job_id}/{node_key} — fix-list #14."""
    prompt: str


class ExecRetryInput(BaseModel):
    """Body for POST /exec/retry — fix-list #14.

    `job_id` kept as str (not UUID) to preserve current 400 'Invalid job_id
    format' error on malformed UUIDs. Pydantic UUID type would return 422
    instead → would break any client parsing the 400 shape.
    """
    job_id: str
    node_key: str

class PromptRevision(BaseModel):
    """Single historical prompt revision (audit items #7.8, #7.9)."""
    revision_number: int
    prompt_text: str
    edited_at: datetime
    edited_by: str | None = None
    source: str = "manual"


class PromptHistoryResponse(BaseModel):
    """Response for GET /prompts/{job_id}/{node_key}/history."""
    job_id: str
    node_key: str
    current_prompt: str
    revision_count: int
    revisions: list[PromptRevision]



# ---------------- Job + research-session management (Phase C) ----------------

class JobRenameInput(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("title must be non-empty")
        if len(v) > 200:
            raise ValueError("title must be 200 characters or fewer")
        return v


class JobCostsBreakdownItem(BaseModel):
    """Sprint J.3.b — one row of the per-(provider, model) cost breakdown."""
    provider: str
    model: str
    calls: int
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class JobCostsKindItem(BaseModel):
    """§17.90 — one row of the per-call_kind cost breakdown.

    ``kind`` is the literal string from ``llm_call_logs.call_kind``
    with NULL folded into the sentinel ``"uncategorized"`` so consumers
    don't have to handle NULL. Currently only ``"synthesis"`` is set
    explicitly (by ``_synthesize_compiled_output``); everything else
    lands in ``"uncategorized"``.
    """
    kind: str
    calls: int
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class JobCostsResponse(BaseModel):
    """Sprint J.3.b — aggregate cost + latency for one job, with breakdown.

    ``by_provider`` is sorted descending by cost_usd then calls so the
    biggest spend lines surface first. ``call_count``/``total_*`` are
    job-wide totals across all (provider, model) combinations.

    §17.90 added ``by_kind`` — same shape, grouped by ``call_kind``.
    Useful for splitting compile-time synthesis spend from execution
    spend (W.7 follow-up).
    """
    job_id: str
    total_cost_usd: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_latency_ms: int
    call_count: int
    by_provider: list[JobCostsBreakdownItem]
    by_kind: list[JobCostsKindItem] = []


class JobSynthesisOverrideInput(BaseModel):
    """Sprint X.6 — per-job opt-in for the W.7 LLM synthesis pass.

    None inherits ``settings.compile_synthesis_enabled``; True forces
    synthesis on for this job; False forces it off. The field is
    explicitly Optional[bool] (not ``bool | None`` with a default) so
    consumers MUST declare intent — sending an absent body would be
    indistinguishable from "set to null".
    """
    override: bool | None


class JobSynthesisOverrideResponse(BaseModel):
    job_id: str
    override: bool | None


class ErrorLogResolveInput(BaseModel):
    """Audit M4 — input for PATCH /observability/errors/{id}.

    ``resolved`` is required so the caller declares intent explicitly.
    ``resolution`` is a free-form note describing the triage decision
    (e.g. "fixed_by: W.6 tool_call migration"); empty / None means
    no note. The endpoint stamps ``resolved_at = NOW()`` when
    resolved=true, and clears it when resolved=false.
    """
    resolved: bool
    resolution: str | None = None


class ErrorLogResolveResponse(BaseModel):
    error_id: str
    resolved: bool
    resolution: str | None
    resolved_at: str | None


class JobSummary(BaseModel):
    id: str
    title: str
    status: str
    node_count: int = 0
    created_at: str
    updated_at: str


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
    total: int
    limit: int
    offset: int


class ResearchSessionRenameInput(BaseModel):
    topic: str

    @field_validator("topic")
    @classmethod
    def _validate_topic(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("topic must be non-empty")
        if len(v) > 500:
            raise ValueError("topic must be 500 characters or fewer")
        return v


class ResearchSessionSummary(BaseModel):
    id: str
    topic: str
    status: str
    depth: str
    domain: str
    iterations_completed: int
    total_entries_ingested: int
    coverage_pct: float | None = None
    created_at: str
    updated_at: str


class ResearchSessionListResponse(BaseModel):
    sessions: list[ResearchSessionSummary]
    total: int
    limit: int
    offset: int


class DeleteResponse(BaseModel):
    deleted: bool
    id: str


# §17.145 — Spec confirmation gate (engineering-design pipeline).

class SpecRead(BaseModel):
    """Serializable view of a row from the ``specs`` table. Used by
    the /specs/* endpoints and by any caller that needs to hand a
    confirmation state to a client."""
    id: UUID
    job_id: UUID | None = None
    schema_version: str
    spec_json: dict[str, Any]
    spec_sha256: str
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime


class SpecPendingListResponse(BaseModel):
    """Response for GET /specs/pending — list of specs awaiting
    operator confirmation."""
    pending: list[SpecRead]
    count: int


# §17.146 — Topology-selection stage (first reasoning step in the
# engineering-design pipeline).

class TopologyCandidateRead(BaseModel):
    """One LLM-proposed topology, with citations into the RAG
    retrieval set. ``citations`` are entry_ids the wrapper has already
    validated against the retrieval set — a response carrying this
    type is guaranteed not to contain hallucinated citations."""
    name: str
    description: str
    rationale: str
    citations: list[str]


class TopologySelectionRead(BaseModel):
    """Response for POST /specs/{spec_id}/topology-select. Carries
    both the candidates and the retrieval-audit columns so a client
    can render the citations as live links into the corpus."""
    id: UUID
    spec_id: UUID
    candidates: list[TopologyCandidateRead]
    rag_chunk_ids: list[str]
    rag_query: str
    rag_domain: str | None = None
    model_used: str
    created_at: datetime


# §17.147 — Device-sizing stage (first closed-loop stage of the
# engineering-design pipeline).

class DeviceSizingRead(BaseModel):
    """Response for POST /topology-selections/{id}/size. Returns the
    persisted device_sizings row whether or not the loop converged —
    ``converged`` is the outcome flag, ``errors`` carries the loop's
    diagnostic. The wider pipeline accepts a sizing as ready only
    when ``converged == True``."""
    id: UUID
    spec_id: UUID
    topology_selection_id: UUID
    candidate_idx: int
    converged: bool
    iterations: int
    final_params: dict[str, str]
    final_netlist: str
    final_measurements: dict[str, float]
    sim_run_ids: list[UUID]
    model_used: str
    errors: list[str]
    created_at: datetime


# §17.148 — Terminal report stage (regenerable-from-artifacts).

class ReportConstraintRead(BaseModel):
    id: str
    kind: str
    description: str
    target: float | None = None
    min: float | None = None
    max: float | None = None
    tolerance_pct: float | None = None
    unit: str
    criticality: str
    measured: float | None = None
    status: str  # ok | out_of_tolerance | violated_min | violated_max | not_measured | skipped


class ReportCitationRead(BaseModel):
    entry_id: str
    title: str = ""
    snippet: str = ""
    source_url: str = ""
    available: bool = False


class ReportSimRunRead(BaseModel):
    sim_run_id: UUID
    iteration: int
    tool: str
    tool_version: str
    exit_code: int
    timed_out: bool
    duration_ms: int
    measurements: dict[str, float]
    verdict: str | None = None


# §17.151 — design_circuit job type (orchestrator front door).

class DesignCreateInput(BaseModel):
    """POST /design body. ``brief`` is the natural-language design
    intent. Optional ``model_role`` lets operators override the
    extractor model (e.g. for offline-only deployments)."""
    brief: str = Field(..., min_length=1, max_length=10000)
    model_role: str | None = None


class DesignAmbiguityRead(BaseModel):
    field: str
    reason: str
    question: str


class DesignCreateResponse(BaseModel):
    """POST /design response. Exactly one of the three result groups
    is non-empty: success (``job_id`` + ``spec_id``), ambiguity
    (``ambiguities`` populated), or extractor error (``errors``)."""
    job_id: UUID | None = None
    spec_id: UUID | None = None
    ambiguities: list[DesignAmbiguityRead] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    model_used: str = ""


class DesignStateRead(BaseModel):
    """GET /design/{job_id} response — aggregated pipeline state."""
    job_id: UUID
    job_type: str
    status: str
    brief: str
    created_at: datetime
    spec_id: UUID | None = None
    spec_confirmed_at: datetime | None = None
    topology_selection_id: UUID | None = None
    device_sizing_id: UUID | None = None
    device_sizing_converged: bool | None = None


class ReportRead(BaseModel):
    """Structured report — a deterministic projection of the audit
    tables for a single device_sizings row. No LLM content, no new
    data beyond what's already attested in the underlying rows."""
    report_schema_version: str
    generated_at: datetime
    sizing_id: UUID
    spec_id: UUID
    topology_selection_id: UUID
    candidate_idx: int
    converged: bool
    iterations: int
    design_name: str
    design_kind: str
    design_description: str
    spec_schema_version: str
    constraints: list[ReportConstraintRead]
    interfaces: list[dict[str, Any]]
    environment: dict[str, Any]
    selected_topology: dict[str, str]
    citations: list[ReportCitationRead]
    final_params: dict[str, str]
    final_netlist: str
    final_measurements: dict[str, float]
    sim_runs: list[ReportSimRunRead]
    errors: list[str]
    model_used: str
