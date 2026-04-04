"""
Step 21: Execution Handler Module
Interactive execution control — status, next-node identification, retry.
"""

import logging
from uuid import UUID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def execution_status(job_id: UUID, db: AsyncSession) -> dict:
    """Get current execution state: job status + all nodes with actionable next."""
    # Job info
    job_result = await db.execute(
        text("SELECT id, title, status, compiled_output FROM jobs WHERE id = :job_id"),
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

    done_keys = {r.node_key for r in rows if r.status == "done"}
    nodes = []
    next_node = None

    for r in rows:
        deps = r.depends_on or []
        deps_met = all(d in done_keys for d in deps)
        is_actionable = r.status in ("pending", "failed") and deps_met

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

    return {
        "job_id": str(job_id),
        "job_title": job.title,
        "job_status": job.status,
        "compiled_output": job.compiled_output,
        "counts": counts,
        "total_nodes": len(nodes),
        "next_node": next_node,
        "nodes": nodes,
    }


async def retry_node(job_id: UUID, node_key: str, db: AsyncSession) -> dict:
    """Reset a failed node back to pending so it can be re-executed."""
    result = await db.execute(
        text("""
            SELECT status FROM dag_nodes
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key}
    )
    row = result.fetchone()

    if not row:
        return {"error": f"Node '{node_key}' not found in job {job_id}"}

    if row.status != "failed":
        return {"error": f"Can only retry failed nodes. '{node_key}' is '{row.status}'."}

    await db.execute(
        text("""
            UPDATE dag_nodes
            SET status = 'pending', output_text = NULL, optimized_prompt = NULL
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key}
    )

    # Also reset job status if it was failed
    await db.execute(
        text("""
            UPDATE jobs SET status = 'executing'
            WHERE id = :job_id AND status = 'failed'
        """),
        {"job_id": str(job_id)}
    )

    await db.commit()
    logger.info("node_reset: node=%s job=%s", node_key, job_id)

    return {
        "job_id": str(job_id),
        "node_key": node_key,
        "reset": True,
        "new_status": "pending",
    }
