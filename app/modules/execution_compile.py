"""Final-output compilation for execution_agent.

No LLM calls. Assembles the deliverable from completed dag_nodes using three
ordered strategies:

  Strategy 0  explicit ``is_output_node`` markers (set by dag_generator from
              the leaf-set of the DAG). One leaf done → that's the deliverable.
              Multiple leaves done → joined with horizontal rules.
  Strategy 2  last terminal-order CodeGen node is the deliverable. Triggers
              for code-producing DAGs that didn't carry an explicit leaf marker.
  Strategy 3  concat-all-done-with-headers. Fallback for partial completion
              and LLM-only DAGs without an explicit leaf marker. Strategy 3
              prepends a "Partial deliverable" preamble so consumers can tell
              this isn't a clean Strategy-0 result, and truncates per-node
              proportionally if the total exceeds settings.compile_output_max_chars.

Empty result (no done node contributed any output) returns ``None`` rather
than ``""`` so callers can store ``compiled_output=NULL`` — the semantically
correct state for "we never produced output" vs. "we produced an empty string".
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.config import settings

logger = logging.getLogger("scaffold.execution_compile")


def _truncate(content: str, max_chars: int) -> str:
    """Mirror execution_agent._truncate_output but local to avoid an import
    cycle. Preserves first/last 20% with a marker in the middle."""
    if len(content) <= max_chars:
        return content
    head = int(max_chars * 0.2)
    tail = int(max_chars * 0.2)
    removed = len(content) - head - tail
    return (
        content[:head]
        + f"\n[...truncated {removed} chars...]\n"
        + content[-tail:]
    )


def _format_section(node: dict) -> str:
    return f"## {node['node_key']}: {node['title']}\n\n{node['output_text']}"


def _join_sections(sections: list[str]) -> str:
    return "\n\n---\n\n".join(sections)


async def _compile_output(job_id: str, db) -> str | None:
    """Compile node outputs into a single deliverable.

    Returns ``None`` when no done node contributed output. Returns the
    compiled string otherwise.
    """
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
    explicit = [
        n for n in nodes
        if n.get("is_output_node") and n["status"] == "done" and n["output_text"]
    ]
    if explicit:
        if len(explicit) == 1:
            return explicit[0]["output_text"]
        return _join_sections([_format_section(n) for n in explicit])

    # Strategy 2: last CodeGen node is the deliverable.
    done = [n for n in nodes if n["status"] == "done" and n["output_text"]]
    if done and done[-1]["tool"] == "CodeGen":
        return done[-1]["output_text"]

    # Strategy 3: concat-all-done-with-headers fallback.
    if not done:
        return None

    # Diagnostic — this path means the DAG produced output but no leaf node
    # was marked. Either the dag_generator's leaf-set logic missed this DAG
    # shape, or the leaf nodes failed/are still pending. Logged so the team
    # can spot patterns over time.
    logger.warning(
        "compile_strategy3_fallback: job=%s done=%d total=%d "
        "(no is_output_node leaf done with output)",
        job_id, len(done), len(nodes),
    )

    sections = [_format_section(n) for n in done]
    body = _join_sections(sections)

    # Apply storage cap. We truncate per-section proportionally so each node
    # keeps representative head/tail content, mirroring the upstream-truncation
    # pattern in execution_agent.
    cap = settings.compile_output_max_chars
    if len(body) > cap:
        # Reserve ~10% of cap for the preamble + section headers + separators.
        budget = max(1000, int(cap * 0.9))
        per_section = max(
            settings.compile_output_min_chunk, budget // max(1, len(sections)),
        )
        truncated_sections = [
            f"## {n['node_key']}: {n['title']}\n\n"
            f"{_truncate(n['output_text'], per_section)}"
            for n in done
        ]
        body = _join_sections(truncated_sections)
        logger.info(
            "compile_strategy3_truncated: job=%s original_chars=%d "
            "truncated_chars=%d per_section_cap=%d",
            job_id, sum(len(s) for s in sections), len(body), per_section,
        )

    preamble = (
        f"_Partial deliverable — {len(done)} of {len(nodes)} node(s) "
        f"contributed. No terminal output node was reached; sections below "
        f"are stitched in execution order._\n\n---\n\n"
    )
    return preamble + body
