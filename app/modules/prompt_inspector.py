"""
Step 20: Prompt Inspector Module
View and edit optimized prompts for DAG nodes.
"""

import logging
from uuid import UUID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def list_prompts(job_id: UUID, db: AsyncSession) -> dict:
    """List all prompts for a job's DAG nodes."""
    result = await db.execute(
        text("""
            SELECT node_key, title, status, execution_order,
                   prompt_template, optimized_prompt
            FROM dag_nodes
            WHERE job_id = :job_id
            ORDER BY execution_order
        """),
        {"job_id": str(job_id)}
    )
    rows = result.fetchall()

    if not rows:
        return {"error": f"No nodes found for job {job_id}"}

    nodes = []
    for r in rows:
        nodes.append({
            "node_key": r.node_key,
            "title": r.title,
            "status": r.status,
            "execution_order": r.execution_order,
            "has_template": bool(r.prompt_template),
            "has_optimized": bool(r.optimized_prompt),
            "template_preview": (r.prompt_template or "")[:120] + ("..." if r.prompt_template and len(r.prompt_template) > 120 else ""),
            "optimized_preview": (r.optimized_prompt or "")[:120] + ("..." if r.optimized_prompt and len(r.optimized_prompt) > 120 else ""),
        })

    return {"job_id": str(job_id), "node_count": len(nodes), "nodes": nodes}


async def get_prompt(job_id: UUID, node_key: str, db: AsyncSession) -> dict:
    """Get full prompt details for a specific node."""
    result = await db.execute(
        text("""
            SELECT node_key, title, status, execution_order,
                   assigned_model, prompt_template, optimized_prompt, output_text
            FROM dag_nodes
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key}
    )
    row = result.fetchone()

    if not row:
        return {"error": f"Node '{node_key}' not found in job {job_id}"}

    return {
        "job_id": str(job_id),
        "node_key": row.node_key,
        "title": row.title,
        "status": row.status,
        "execution_order": row.execution_order,
        "assigned_model": row.assigned_model,
        "prompt_template": row.prompt_template or "",
        "optimized_prompt": row.optimized_prompt or "",
        "has_output": bool(row.output_text),
        "output_preview": (row.output_text or "")[:200] + ("..." if row.output_text and len(row.output_text) > 200 else ""),
    }


async def update_prompt(job_id: UUID, node_key: str, new_prompt: str, db: AsyncSession) -> dict:
    """Update the optimized prompt for a node. Only allowed on pending/failed nodes."""
    # Check current status
    result = await db.execute(
        text("""
            SELECT status, optimized_prompt, prompt_template
            FROM dag_nodes
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key}
    )
    row = result.fetchone()

    if not row:
        return {"error": f"Node '{node_key}' not found in job {job_id}"}

    if row.status not in ("pending", "failed"):
        return {"error": f"Cannot edit prompt for node in '{row.status}' state. Only pending/failed nodes can be edited."}

    old_prompt = row.optimized_prompt or row.prompt_template or ""

    await db.execute(
        text("""
            UPDATE dag_nodes
            SET optimized_prompt = :new_prompt
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key, "new_prompt": new_prompt}
    )
    await db.commit()

    logger.info("prompt_updated: node=%s job=%s", node_key, job_id)

    return {
        "job_id": str(job_id),
        "node_key": node_key,
        "updated": True,
        "old_length": len(old_prompt),
        "new_length": len(new_prompt),
    }
