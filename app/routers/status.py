"""Job status and execution log endpoints.

GET /status            — status counts + recent jobs with node counts
GET /logs/{job_id}     — per-node execution history for a single job
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.database import get_db
from app.modules.recovery import next_actions_for
from app.schemas import JobStatus
from app.web.routes import phase_label_for

logger = logging.getLogger("scaffold.routers.status")
router = APIRouter()


# ── Canonical job status enum ─────────────────────────────────────────
# JobStatus / JOB_STATUSES are the single source of truth (app/schemas.py),
# mirroring the jobs_status_check CHECK constraint. §17.561 — this router
# previously redeclared its own Literal that drifted (missing 'aggregating'),
# which silently dropped umbrella jobs from /status counts and 422'd
# ?status=aggregating. Importing the canonical enum makes drift impossible;
# see tests/test_status_endpoint.py::test_status_enum_parity.
# §17.611 (audit #21) — removed the dead `_TERMINAL_STATUSES` frozenset. It was
# referenced nowhere and its "drives /work" comment was false (get_work hardcodes
# NOT IN ('completed','failed','cancelled') inline), so editing it wouldn't have
# propagated — a single-source-of-truth foot-gun with no runtime effect.


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
    # §17.561 — umbrella jobs live in 'aggregating' while their component
    # children run. Was missing here, so umbrella counts were silently
    # discarded by the valid_keys filter in get_status. Parity with
    # app.schemas.JOB_STATUSES is asserted in test_status_endpoint.py.
    aggregating: int = 0
    # §17.624 — hands-on assist gate parks a predominantly-Shell/human job here
    # (plan generated, nodes pending, operator drives it via /assist). Parity
    # with app.schemas.JOB_STATUSES is asserted in test_status_logs.py.
    awaiting_assist: int = 0


class RecentJobSummary(BaseModel):
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
    recent_jobs: list[RecentJobSummary]
    timestamp: str


# ── §17.561 — "my active work" models (GET /work) ─────────────────────
class WorkJob(BaseModel):
    id: str
    title: str = ""
    status: str
    phase: str
    job_type: str = "legacy"
    node_count: int = 0
    updated_at: Optional[str] = None
    next_actions: list[dict[str, Any]] = []


class WorkAssistSession(BaseModel):
    session_id: str
    job_id: str
    job_title: str = ""
    status: str
    current_node_key: Optional[str] = None
    last_activity_at: Optional[str] = None


class WorkResponse(BaseModel):
    jobs: list[WorkJob] = []
    assist_sessions: list[WorkAssistSession] = []
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
               COALESCE(n.node_count, 0) AS node_count,
               s.id AS session_id
        FROM jobs j
        LEFT JOIN (
            SELECT job_id, COUNT(*) AS node_count
            FROM dag_nodes GROUP BY job_id
        ) n ON n.job_id = j.id
        -- §17.599 — active assist session for assisted_* recovery links.
        LEFT JOIN LATERAL (
            SELECT id FROM assist_sessions
            WHERE job_id = j.id AND status IN ('active', 'paused')
            ORDER BY last_activity_at DESC LIMIT 1
        ) s ON TRUE
    """
    params: dict = {"limit": limit}
    if status_filter:
        query += " WHERE j.status = :status_filter"
        params["status_filter"] = status_filter
    query += " ORDER BY j.updated_at DESC LIMIT :limit"

    jobs_result = await db.execute(text(query), params)
    recent_jobs = [
        RecentJobSummary(
            id=str(row.id),
            title=row.title or "",
            status=row.status,
            node_count=row.node_count,
            created_at=row.created_at.isoformat() if row.created_at else None,
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
            next_actions=next_actions_for(
                row.status, str(row.id),
                session_id=str(row.session_id) if row.session_id else None,
            ),
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


@router.get("/work")
async def get_work(db=Depends(get_db)) -> WorkResponse:
    """List the user's active (non-terminal) work in one response.

    Single-user "you-are-here" primitive backing the pipeline's /here,
    /resume, and /next verbs (§17.561). Returns non-terminal jobs (newest
    first) and active/paused assist sessions, each with a human phase label
    and pre-filled next_actions — so the user never needs a UUID to resume.
    Replaces the per-replica, chat-scoped recall the pipeline relied on
    (OWUI doesn't reliably deliver chat_id).
    """
    # 1. Non-terminal jobs, newest first, with node counts + recovery actions.
    job_rows = await db.execute(
        text("""
            SELECT j.id, j.title, j.status, j.job_type, j.error_summary,
                   j.updated_at, COALESCE(n.node_count, 0) AS node_count,
                   s.id AS session_id
            FROM jobs j
            LEFT JOIN (
                SELECT job_id, COUNT(*) AS node_count
                FROM dag_nodes GROUP BY job_id
            ) n ON n.job_id = j.id
            -- §17.599 — the active assist session (if any) so assisted_* jobs'
            -- {session_id} recovery links resolve to the real session id, not
            -- the job id (they differ; the job-id link 404s).
            LEFT JOIN LATERAL (
                SELECT id FROM assist_sessions
                WHERE job_id = j.id AND status IN ('active', 'paused')
                ORDER BY last_activity_at DESC
                LIMIT 1
            ) s ON TRUE
            WHERE j.status NOT IN ('completed', 'failed', 'cancelled')
            ORDER BY j.updated_at DESC
        """),
    )
    jobs = [
        WorkJob(
            id=str(row.id),
            title=row.title or "",
            status=row.status,
            phase=phase_label_for(row.status),
            job_type=row.job_type or "legacy",
            node_count=row.node_count,
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
            next_actions=next_actions_for(
                row.status, str(row.id), error_summary=row.error_summary,
                session_id=str(row.session_id) if row.session_id else None,
            ),
        )
        for row in job_rows
    ]

    # 2. Active/paused assist sessions, joined to the job title.
    sess_rows = await db.execute(
        text("""
            SELECT s.id, s.job_id, s.status, s.current_node_key,
                   s.last_activity_at, j.title
            FROM assist_sessions s
            JOIN jobs j ON j.id = s.job_id
            WHERE s.status IN ('active', 'paused')
            ORDER BY s.last_activity_at DESC
        """),
    )
    sessions = [
        WorkAssistSession(
            session_id=str(row.id),
            job_id=str(row.job_id),
            job_title=row.title or "",
            status=row.status,
            current_node_key=row.current_node_key,
            last_activity_at=(
                row.last_activity_at.isoformat() if row.last_activity_at else None
            ),
        )
        for row in sess_rows
    ]

    logger.info(
        "work_queried jobs=%d assist_sessions=%d", len(jobs), len(sessions)
    )
    return WorkResponse(
        jobs=jobs,
        assist_sessions=sessions,
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
            -- §17.611 (audit #5) — order by the canonical integer execution_order
            -- (node_key tiebreak), matching get_dag/execution_handler. node_key is
            -- TEXT (T1..Tn); lexical order put T10/T11 before T2, so jobs with 10+
            -- nodes rendered out of order AND the LIMIT/OFFSET scrambled pages.
            ORDER BY execution_order, node_key
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
