"""
Step 21: Execution Handler Module
Interactive execution control — status, next-node identification, retry, resume.
"""

import logging
from typing import Literal
from uuid import UUID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.recovery import next_actions_for

logger = logging.getLogger("scaffold.execution_handler")


async def execution_status(job_id: UUID, db: AsyncSession) -> dict:
    """Get current execution state: job status + all nodes with actionable next."""
    # Job info
    # Sprint X.2 — `compiled_output_synthesized` surfaces whether the
    # stored compiled_output is the LLM-synthesized narrative (W.7) or
    # the raw heuristic body. Lets clients render a "synthesized by LLM"
    # badge when appropriate.
    # Sprint X.6 — `compile_synthesis_override` surfaces the current per-
    # job opt-in knob (NULL = inherit settings.compile_synthesis_enabled).
    # Distinct semantic from `synthesized` above: the override describes
    # the decision *for the next compile*, while `synthesized` records
    # what *the last compile actually did*.
    job_result = await db.execute(
        text(
            "SELECT id, title, status, compiled_output, "
            "       compiled_output_synthesized, "
            "       compile_synthesis_override, "
            "       error_summary, completed_at, job_type "
            "FROM jobs WHERE id = :job_id"
        ),
        {"job_id": str(job_id)}
    )
    job = job_result.fetchone()
    if not job:
        return {"error": f"Job {job_id} not found"}

    # §17.528 — an umbrella (task decomposition) has no DAG; report the
    # rollup of its component children instead of an empty node view.
    if getattr(job, "job_type", "legacy") == "umbrella":
        return await _umbrella_status(job_id, job, db)

    # All nodes
    nodes_result = await db.execute(
        text("""
            SELECT node_key, title, status, execution_order, depends_on,
                   assigned_model, last_verification_reason,
                   COALESCE(is_deliverable, FALSE) AS is_deliverable,
                   confidence, tool
            FROM dag_nodes
            WHERE job_id = :job_id
            ORDER BY execution_order
        """),
        {"job_id": str(job_id)}
    )
    rows = nodes_result.fetchall()

    # #7.6 — "skipped" nodes satisfy downstream deps_met; otherwise skipping
    # a single node would lock the whole downstream chain forever.
    satisfied_keys = {r.node_key for r in rows if r.status in ("done", "skipped")}
    nodes = []
    next_node = None

    for r in rows:
        deps = r.depends_on or []
        deps_met = all(d in satisfied_keys for d in deps)
        # #7.7 — only "pending" is actionable via /execute. Failed nodes
        # require an explicit /exec/retry — "actionable" must mean "something
        # /execute will pick up," and /execute never re-picks failed without
        # explicit retry.
        is_actionable = r.status == "pending" and deps_met

        node = {
            "node_key": r.node_key,
            "title": r.title,
            "status": r.status,
            "execution_order": r.execution_order,
            "depends_on": deps,
            "deps_met": deps_met,
            "actionable": is_actionable,
            "assigned_model": r.assigned_model,
            # §17.450 (Phase B / B3) — surface WHY a node failed to the web +
            # CLI exec-status consumers (dag_nodes.last_verification_reason).
            "failure_reason": r.last_verification_reason,
            # §17.480 — surface the node-overhaul signals to the web detail
            # page: the §17.475 deliverable marker, §17.477 verifier
            # confidence, and the tool so the UI can badge each node.
            "is_deliverable": bool(r.is_deliverable),
            "confidence": r.confidence,
            "tool": r.tool,
        }
        nodes.append(node)

        if is_actionable and next_node is None:
            next_node = node

    counts = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1

    # Audit item 10: surface the per-status next-action registry as a
    # structured field in the response. Pick the most informative
    # node_key for substitution — failed nodes drive retry/skip
    # suggestions; blocked next; running last as a stuck-node hint.
    failed_node = next(
        (n["node_key"] for n in nodes if n["status"] == "failed"), None,
    )
    blocked_node = next(
        (n["node_key"] for n in nodes
         if n["status"] == "pending" and not n["deps_met"]), None,
    )
    running_node = next(
        (n["node_key"] for n in nodes if n["status"] == "running"), None,
    )
    actions = next_actions_for(
        job.status,
        str(job_id),
        failed_node_key=failed_node,
        blocked_node_key=blocked_node,
        running_node_key=running_node,
        error_summary=getattr(job, "error_summary", None),
    )

    # Sprint J.3.b — surface a lightweight cost/latency totals block.
    # Single SUM query against llm_call_logs; zero shape if telemetry
    # hasn't recorded anything for this job yet (fail-open). The
    # detailed per-(provider, model) breakdown lives at the dedicated
    # /jobs/{id}/costs endpoint; /exec/status keeps it summary-only so
    # the hot status path stays cheap.
    from app.modules.cost_rollup import get_job_cost_totals
    cost_totals = await get_job_cost_totals(str(job_id), db)

    return {
        "job_id": str(job_id),
        "job_title": job.title,
        "job_status": job.status,
        "error_summary": getattr(job, "error_summary", None),
        # §17.467 — surface the §17.466 completion timestamp to the web detail
        # page + SDK/CLI. None until the job reaches a terminal state (the
        # trg_jobs_completed_at invariant); ISO-8601 string when set. getattr
        # (not job.completed_at) mirrors the error_summary access above so a
        # SimpleNamespace mock row that omits the column degrades to None
        # instead of AttributeError.
        "completed_at": (
            _completed_at.isoformat()
            if (_completed_at := getattr(job, "completed_at", None))
            else None
        ),
        "compiled_output": job.compiled_output,
        "synthesized": bool(job.compiled_output_synthesized),
        "synthesis_override": job.compile_synthesis_override,
        "counts": counts,
        "total_nodes": len(nodes),
        "next_node": next_node,
        "next_actions": actions,
        "nodes": nodes,
        "costs": cost_totals,
    }


async def _umbrella_status(job_id: UUID, job, db: AsyncSession) -> dict:
    """§17.528 — rollup view for an umbrella (decomposition parent): its
    component children and their live statuses. Keeps the standard
    ``execution_status`` keys (nodes=[], counts={}, …) so existing SDK/pipeline
    readers don't KeyError, and adds ``job_type='umbrella'`` + ``children``."""
    rows = (await db.execute(
        text("""
            SELECT id, title, status, component_index
            FROM jobs WHERE parent_job_id = :u
            ORDER BY component_index
        """),
        {"u": str(job_id)},
    )).fetchall()
    children = [{
        "job_id": str(r.id),
        "title": r.title,
        "status": r.status,
        "component_index": r.component_index,
    } for r in rows]
    return {
        "job_id": str(job_id),
        "job_title": job.title,
        "job_status": job.status,
        "job_type": "umbrella",
        "error_summary": getattr(job, "error_summary", None),
        "completed_at": (
            _completed_at.isoformat()
            if (_completed_at := getattr(job, "completed_at", None))
            else None
        ),
        "compiled_output": None,
        "synthesized": False,
        "synthesis_override": None,
        "children": children,
        "children_total": len(children),
        "children_completed": sum(1 for c in children if c["status"] == "completed"),
        "counts": {},
        "total_nodes": 0,
        "next_node": None,
        "next_actions": [],
        "nodes": [],
    }


async def node_outputs(job_id: UUID, db: AsyncSession) -> dict:
    """Per-node output text for a job — backs ``GET /exec/nodes/{job_id}``.

    §17.471 — ``/exec/status`` is deliberately summary-only (the hot
    status path: counts + node statuses, no output bodies), and the
    compiled deliverable can omit most nodes: ``execution_compile``
    Strategy 0 joins only the ``is_output_node`` DAG leaves, so a 10-node
    job whose leaf-set is ``{T4, T10}`` produces a ``compiled_output``
    containing just those two. Operators who wanted every node's full
    work product (T1..Tn) had no way to retrieve it from chat — neither
    ``/results`` (compiled deliverable) nor ``/exec status`` (status
    table, no bodies) surfaced it.

    This returns each node's ``output_text`` verbatim plus its
    ``is_output_node`` flag (which ``/exec/status`` also omits) so the
    ``scaffold_router`` ``/results <job_id> nodes`` view can render each
    node individually and mark which ones fed the compiled deliverable.
    """
    job_result = await db.execute(
        text("SELECT id, title, status FROM jobs WHERE id = :job_id"),
        {"job_id": str(job_id)},
    )
    job = job_result.fetchone()
    if not job:
        return {"error": f"Job {job_id} not found"}

    nodes_result = await db.execute(
        text("""
            SELECT node_key, title, status, execution_order,
                   is_output_node, COALESCE(is_deliverable, FALSE) AS is_deliverable,
                   output_text
            FROM dag_nodes
            WHERE job_id = :job_id
            ORDER BY execution_order, node_key
        """),
        {"job_id": str(job_id)},
    )
    rows = nodes_result.fetchall()

    nodes = []
    for r in rows:
        out = r.output_text or ""
        nodes.append({
            "node_key": r.node_key,
            "title": r.title,
            "status": r.status,
            "execution_order": r.execution_order,
            "is_output_node": bool(r.is_output_node),
            # §17.475 — the model-asserted deliverable marker; lets the
            # /results <id> nodes view distinguish THE deliverable from leaves.
            "is_deliverable": bool(r.is_deliverable),
            "output_text": out,
            "output_len": len(out),
        })

    return {
        "job_id": str(job_id),
        "job_title": job.title,
        "job_status": job.status,
        "total_nodes": len(nodes),
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# Resume — cancelled → executing
# ---------------------------------------------------------------------------

ResumeOutcome = Literal["resumed", "not_found", "wrong_status"]


async def resume_cancelled_job(
    job_id: UUID, db: AsyncSession,
) -> dict:
    """Atomically transition a cancelled job back to ``executing``.

    The UPDATE is gated on ``status = 'cancelled'``; two concurrent
    callers cannot both win because the WHERE clause matches at most
    one row state. Returns:

    - ``{"outcome": "resumed", "job_id": str, "prior_status": "cancelled"}`` on success
    - ``{"outcome": "wrong_status", "job_id": str, "current_status": str}`` when the
      job exists but is not cancelled (already executing, completed,
      failed, etc.)
    - ``{"outcome": "not_found", "job_id": str}`` when no row matches the ID

    Resumption is intentionally idempotent at the data layer:
    ``execute_all_nodes`` is already idempotent over completed nodes (it
    uses their outputs as upstream context for downstream nodes), so the
    caller can re-fire ``/execute/all`` after this returns ``resumed``
    and pick up from the last pending node.
    """
    result = await db.execute(
        text(
            "UPDATE jobs "
            "SET status = 'executing', updated_at = NOW() "
            "WHERE id = :job_id AND status = 'cancelled' "
            "RETURNING id"
        ),
        {"job_id": str(job_id)},
    )
    row = result.fetchone()
    if row is not None:
        await db.commit()
        logger.info("job_resumed job_id=%s", job_id)
        return {
            "outcome": "resumed",
            "job_id": str(job_id),
            "prior_status": "cancelled",
        }

    # No row updated. Two reasons: (a) job doesn't exist, (b) job exists
    # but isn't cancelled. Distinguish so the caller can emit the right
    # HTTP status.
    await db.rollback()
    status_result = await db.execute(
        text("SELECT status FROM jobs WHERE id = :job_id"),
        {"job_id": str(job_id)},
    )
    current = status_result.fetchone()
    if current is None:
        return {"outcome": "not_found", "job_id": str(job_id)}
    return {
        "outcome": "wrong_status",
        "job_id": str(job_id),
        "current_status": current.status,
    }


# ---------------------------------------------------------------------------
# §17.322 — operator-driven job cancellation (symmetric to resume above)
# ---------------------------------------------------------------------------

# Statuses where ``cancel`` is a no-op (already in a terminal state where
# transitioning to ``cancelled`` is either wrong or already done).
# ``cancelled`` itself is intentionally NOT here — repeating /cancel on an
# already-cancelled job is idempotent OK, not 409.
_NON_CANCELLABLE_STATUSES: tuple[str, ...] = ("completed", "failed")


async def cancel_active_job(
    job_id: UUID, db: AsyncSession,
) -> dict:
    """Atomically transition a non-terminal job to ``cancelled``.

    The UPDATE is gated on ``status NOT IN ('completed','failed','cancelled')``
    so two concurrent /cancel calls cannot both win and so an
    already-terminal job is not mutated. Returns:

    - ``{"outcome": "cancelled", "job_id": str, "status_before": <prior>}``
      on a successful active→cancelled flip
    - ``{"outcome": "already_cancelled", "job_id": str}`` when the job
      was already ``cancelled``; idempotent OK at the data layer (the
      router maps this to 200 with a different message)
    - ``{"outcome": "wrong_status", "job_id": str, "current_status": str}``
      when the job is in a terminal non-cancellable state
      (``completed`` or ``failed``)
    - ``{"outcome": "not_found", "job_id": str}`` when no row matches

    **Why the existing ``_cancel_job`` in ideation_workflow.py isn't reused.**
    That helper is a fire-and-forget UPDATE used only on the Phase-2
    client_disconnect path; it has no status guard, no outcome
    discrimination, and writes ``error_summary``. The operator-driven
    /cancel path needs the three-outcome shape (so the router can map
    400/404/409 + idempotent 200) AND must NOT clobber error_summary on
    jobs that have a real failure reason already stored.

    **Concurrency.** Two concurrent /cancel calls on the same job: only
    one matches the WHERE clause; the other falls through to the
    ``already_cancelled`` branch via the post-rollback status SELECT.
    A /cancel racing an ``/execute/all`` SSE worker: the worker's next
    DB write (status='running' on a node-complete) sees the cancellation
    via the status check at the top of the execution loop — see
    execute_all_nodes' precondition probe.
    """
    # CTE captures the prior status atomically with the UPDATE — needed
    # because UPDATE ... RETURNING reflects the NEW row, not the old.
    # The FOR UPDATE inside the CTE serializes concurrent /cancel calls
    # so the second caller falls through to the already_cancelled branch.
    result = await db.execute(
        text(
            "WITH prior AS ("
            "  SELECT id, status AS prior_status FROM jobs "
            "  WHERE id = :job_id FOR UPDATE"
            ") "
            "UPDATE jobs "
            "SET status = 'cancelled', updated_at = NOW() "
            "FROM prior "
            "WHERE jobs.id = prior.id "
            "  AND jobs.status NOT IN ('completed','failed','cancelled') "
            "RETURNING jobs.id, prior.prior_status"
        ),
        {"job_id": str(job_id)},
    )
    row = result.fetchone()
    if row is not None:
        await db.commit()
        logger.info(
            "job_cancelled job_id=%s prior_status=%s",
            job_id, row.prior_status,
        )
        return {
            "outcome": "cancelled",
            "job_id": str(job_id),
            "status_before": row.prior_status,
            "status_after": "cancelled",
        }

    # No row updated. Three reasons: (a) job doesn't exist, (b) job
    # exists and is already cancelled (idempotent OK), (c) job exists
    # and is in a terminal non-cancellable state. Distinguish via a
    # post-rollback status SELECT.
    await db.rollback()
    status_result = await db.execute(
        text("SELECT status FROM jobs WHERE id = :job_id"),
        {"job_id": str(job_id)},
    )
    current = status_result.fetchone()
    if current is None:
        return {"outcome": "not_found", "job_id": str(job_id)}
    if current.status == "cancelled":
        return {
            "outcome": "already_cancelled",
            "job_id": str(job_id),
            "status_before": "cancelled",
            "status_after": "cancelled",
        }
    # status is 'completed' or 'failed' — terminal, non-cancellable.
    return {
        "outcome": "wrong_status",
        "job_id": str(job_id),
        "current_status": current.status,
    }
