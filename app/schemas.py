"""Scaffold Engine — Pydantic schemas for all 8 tables.

Each table has three schema variants:
  - Base:     shared fields (used for creation input)
  - Create:   alias for Base (explicit intent)
  - Read:     includes id, timestamps, and DB-generated fields

Step 6 of 23-step build plan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

JobStatus = Literal[
    "pending", "refining", "awaiting_confirmation", "researching", "planning", "executing",
    "completed", "failed", "cancelled",
]

NodeStatus = Literal["pending", "running", "done", "failed", "skipped"]

NodeType = Literal["task", "decision", "parallel_group", "checkpoint"]

LogLevel = Literal["debug", "info", "warning", "error", "critical"]

ErrorType = Literal[
    "transient", "model_failure", "timeout",
    "validation", "structural", "unrecoverable",
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

    model_config = {"from_attributes": True}


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

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 7. Benchmark Results
# ---------------------------------------------------------------------------

class BenchmarkBase(BaseModel):
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

    model_config = {"from_attributes": True}


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
    prompt: str
    model_optimizer: str | None = None
    model_verifier: str | None = None
    skip_verify: bool = False

class PromptOptimizeResult(BaseModel):
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
    job_id: str
    skip_optimize: bool = False
    skip_verify: bool = False
    model_override: str | None = None

class SkipNodeInput(BaseModel):
    job_id: str
    node_key: str

class ExecutionResult(BaseModel):
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
    error: str | None = None
