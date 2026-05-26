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
            "       error_summary "
            "FROM jobs WHERE id = :job_id"
        ),
        {"job_id": str(job_id)}
    )
    job = job_result.fetchone()
    if not job:
        return {"error": f"Job {job_id} not found"}

    # All nodes
    nodes_result = await db.execute(
        text("""
            SELECT node_key, title, status, execution_order, depends_on, assigned_model
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
