"""
Step 21: Execution Handler Module
Interactive execution control — status, next-node identification, retry.
"""

import logging
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
            "       compile_synthesis_override "
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
