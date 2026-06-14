"""Job status and execution log endpoints.

GET /status            — status counts + recent jobs with node counts
GET /logs/{job_id}     — per-node execution history for a single job
"""

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.database import get_db
from app.modules.recovery import next_actions_for

logger = logging.getLogger("scaffold.routers.status")
router = APIRouter()


# ── Canonical job status enum ─────────────────────────────────────────
# Must mirror the jobs_status_check CHECK constraint in the database.
JobStatus = Literal[
    "pending",
    "refining",
    "awaiting_confirmation",
    "researching",
    "planning",
    "executing",
    "running",
    "completed",
    "failed",
    "cancelled",
    "blocked",
    "assisted_executing",
    "assisted_running",
    "assisted_paused",
]


# ── Pydantic response models ──────────────────────────────────────────
class StatusCounts(BaseModel):
    pending: int = 0
    refining: int = 0
    awaiting_confirmation: int = 0
    researching: int = 0
    planning: int = 0
    executing: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    blocked: int = 0
    assisted_executing: int = 0
    assisted_running: int = 0
    assisted_paused: int = 0


class JobSummary(BaseModel):
    id: str
    title: str = ""
    status: str
    node_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    next_actions: list[dict[str, Any]] = []


class StatusResponse(BaseModel):
    status_counts: StatusCounts
    total_jobs: int
    recent_jobs: list[JobSummary]
    timestamp: str


class NodeLog(BaseModel):
    node_key: str
    title: str
    tool: str
    status: str
    domain: Optional[str] = None
    output_preview: Optional[str] = None
    confidence: Optional[float] = None
    updated_at: Optional[str] = None
    # §17.445 (Phase A / A1) — surface WHY a node failed. dag_nodes.last_
    # verification_reason (mig 026) was written on failure but never exposed by
    # any read API, so a failed job showed no "why" without grepping Postgres.
    failure_reason: Optional[str] = None


class LogsResponse(BaseModel):
    job_id: str
    job_status: str
    node_count: int
    nodes: list[NodeLog]
    limit: int
    offset: int
    compiled_output: Optional[str] = None
    # §17.519 — machine-readable deliverable kind: 'executed' | 'plan_only' |
    # 'assist_completed' | None. Lets clients branch on whether the work was
    # actually performed without parsing the compiled_output banner text.
    deliverable_kind: Optional[str] = None
    timestamp: str


def _require_uuid(raw: str, field: str = "job_id") -> str:
    """Validate a path param as a UUID; 400 on malformed."""
    try:
        return str(UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"{field} must be a UUID")


# ── Endpoints ──────────────────────────────────────────────────────────
@router.get("/status")
async def get_status(
    limit: int = Query(default=20, ge=1, le=100),
    status_filter: Optional[JobStatus] = Query(default=None, alias="status"),
    db=Depends(get_db),
) -> StatusResponse:
    """Return job status counts and recent jobs."""
    # 1. Status counts
    count_result = await db.execute(
        text("SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status")
    )
    counts = {row.status: row.cnt for row in count_result}
    valid_keys = set(StatusCounts.model_fields.keys())
    status_counts = StatusCounts(**{k: counts.get(k, 0) for k in valid_keys})

    # 2. Recent jobs with node counts + per-row next_actions.
    # j.title is the human-readable label shown in every list surface
    # (OWUI /status, `make status`, CLI tables); historically omitted
    # here so the renderers showed bare UUIDs.
    query = """
        SELECT j.id, j.title, j.status, j.created_at, j.updated_at,
               COALESCE(n.node_count, 0) AS node_count
        FROM jobs j
        LEFT JOIN (
            SELECT job_id, COUNT(*) AS node_count
            FROM dag_nodes GROUP BY job_id
        ) n ON n.job_id = j.id
    """
    params: dict = {"limit": limit}
    if status_filter:
        query += " WHERE j.status = :status_filter"
        params["status_filter"] = status_filter
    query += " ORDER BY j.updated_at DESC LIMIT :limit"

    jobs_result = await db.execute(text(query), params)
    recent_jobs = [
        JobSummary(
            id=str(row.id),
            title=row.title or "",
            status=row.status,
            node_count=row.node_count,
            created_at=row.created_at.isoformat() if row.created_at else None,
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
            next_actions=next_actions_for(row.status, str(row.id)),
        )
        for row in jobs_result
    ]

    total = sum(counts.values())
    logger.info(
        "status_queried total_jobs=%d recent_returned=%d status_filter=%s",
        total, len(recent_jobs), status_filter,
    )
    return StatusResponse(
        status_counts=status_counts,
        total_jobs=total,
        recent_jobs=recent_jobs,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/logs/{job_id}")
async def get_logs(
    job_id: str,
    include_output: bool = Query(default=False),
    include_compiled: bool = Query(
        default=False,
        description="Include jobs.compiled_output in the response.",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db=Depends(get_db),
) -> LogsResponse:
    """Return per-node execution history for a job (paginated)."""
    job_id = _require_uuid(job_id, field="job_id")

    # 1. Verify job exists, get status + (optional) compiled output
    job_result = await db.execute(
        text("SELECT status, compiled_output, deliverable_kind "
             "FROM jobs WHERE id = :job_id"),
        {"job_id": job_id},
    )
    job_row = job_result.first()
    if not job_row:
        raise HTTPException(status_code=404, detail="Job not found")

    # 2. Total node count (for pagination metadata)
    count_row = await db.execute(
        text("SELECT COUNT(*) AS cnt FROM dag_nodes WHERE job_id = :job_id"),
        {"job_id": job_id},
    )
    total_nodes = count_row.scalar() or 0

    # 3. Node-level execution details, paginated
    nodes_result = await db.execute(
        text("""
            SELECT node_key, title, tool, status, domain,
                   output_text, confidence, updated_at,
                   last_verification_reason
            FROM dag_nodes
            WHERE job_id = :job_id
            ORDER BY node_key
            LIMIT :limit OFFSET :offset
        """),
        {"job_id": job_id, "limit": limit, "offset": offset},
    )
    nodes = []
    for row in nodes_result:
        preview = None
        if row.output_text:
            if include_output:
                preview = row.output_text
            else:
                preview = (
                    row.output_text[:500] + "…"
                    if len(row.output_text) > 500
                    else row.output_text
                )
        nodes.append(
            NodeLog(
                node_key=row.node_key,
                title=row.title,
                tool=row.tool,
                status=row.status,
                domain=row.domain,
                output_preview=preview,
                confidence=row.confidence,
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
                failure_reason=row.last_verification_reason,
            )
        )

    logger.info(
        "logs_queried job_id=%s job_status=%s node_count=%d returned=%d limit=%d offset=%d",
        job_id, job_row.status, total_nodes, len(nodes), limit, offset,
    )
    return LogsResponse(
        job_id=job_id,
        job_status=job_row.status,
        node_count=total_nodes,
        nodes=nodes,
        limit=limit,
        offset=offset,
        compiled_output=job_row.compiled_output if include_compiled else None,
        deliverable_kind=job_row.deliverable_kind,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
