"""Final-output compilation for execution_agent.

No LLM calls — assembles the deliverable from completed dag_nodes using three
ordered strategies: explicit ``is_output_node`` markers, last CodeGen node, or
concatenated outputs with headers.
"""
from __future__ import annotations

from sqlalchemy import text


async def _compile_output(job_id: str, db) -> str:
    """Compile node outputs into a single deliverable. No LLM calls."""
    rows = await db.execute(
        text(
            "SELECT node_key, title, tool, status, output_text, "
            "       COALESCE(is_output_node, FALSE) AS is_output_node "
            "FROM dag_nodes WHERE job_id = :jid ORDER BY execution_order"
        ),
        {"jid": job_id},
    )
    nodes = rows.mappings().all()

    # Strategy 0 (#97): explicit is_output_node marker wins over heuristics.
    explicit = [n for n in nodes if n.get("is_output_node") and n["status"] == "done" and n["output_text"]]
    if explicit:
        if len(explicit) == 1:
            return explicit[0]["output_text"]
        return "\n\n---\n\n".join(
            f"## {n['node_key']}: {n['title']}\n\n{n['output_text']}"
            for n in explicit
        )

    # Strategy 2: last CodeGen node is the deliverable
    done = [n for n in nodes if n["status"] == "done" and n["output_text"]]
    if done and done[-1]["tool"] == "CodeGen":
        return done[-1]["output_text"]

    # Strategy 3: concatenate all passed outputs with headers
    parts = []
    for n in nodes:
        if n["status"] == "done" and n["output_text"]:
            parts.append(f"## {n['node_key']}: {n['title']}\n\n{n['output_text']}")
    return "\n\n---\n\n".join(parts)
