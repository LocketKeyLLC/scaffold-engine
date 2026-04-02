"""Scaffold Engine — DAG generator module.

Takes a refined brief (from Step 10) → LLM decomposition → validated DAG.
Reuses Workflow Architect validation logic:
  - Kahn-based cycle detection
  - Strategy inference (sequential/parallel/hybrid/conditional)
  - I/O contract auditing

Persists nodes to dag_nodes table. Job transitions: planning → executing.

Step 11 of 23-step build plan.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router

logger = logging.getLogger("scaffold.dag")

# ---------------------------------------------------------------------------
# Valid enums (mirrored from WA tool)
# ---------------------------------------------------------------------------

VALID_TASK_TYPES = {"research", "decision", "action", "validation", "output", "human_review"}
VALID_STRATEGIES = {"sequential", "parallel", "hybrid", "conditional"}
VALID_TOOLS = {"LLM", "CodeGen", "Human", "FileSystem", "SearXNG", "Milvus"}

# ---------------------------------------------------------------------------
# DAG generation prompt
# ---------------------------------------------------------------------------

DAG_SYSTEM = """You are a workflow decomposition engine. Given a structured brief, produce a DAG of executable tasks.

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "strategy": "sequential | parallel | hybrid | conditional",
  "tasks": [
    {
      "id": "T1",
      "name": "max 5 words",
      "type": "research | decision | action | validation | output | human_review",
      "inputs": ["what this task consumes"],
      "outputs": ["what this task produces"],
      "depends_on": [],
      "tool": "LLM | SearXNG | Milvus | CodeGen | Human | FileSystem",
      "assigned_model": "model name or null",
      "notes": "optional execution hint"
    }
  ]
}

Rules:
- Minimum 2 tasks, maximum 15
- Every task must have a unique id (T1, T2, ...)
- depends_on references other task ids
- No circular dependencies
- First task(s) must have empty depends_on
- Last task(s) must be type "output" or "validation"
- Keep task names to max 5 words
- Tool guide:
  * Milvus = ALWAYS use when the task involves the knowledge base, KB, internal docs, TOON files, or domain-specific lookup. Any mention of "knowledge base", "KB", "look up from", "retrieve from", or stored/internal knowledge MUST use Milvus, NEVER SearXNG.
  * SearXNG = web search for EXTERNAL, current, or live information NOT in the knowledge base.
  * CodeGen = code generation or script writing.
  * FileSystem = file write/read/save operations.
  * Human = human review or approval gate.
  * LLM = general reasoning, summarization, analysis (default for everything else).
- If complexity is high or ambiguities exist, include a human_review task"""

DAG_PROMPT = """Decompose this refined brief into a DAG of executable tasks:

---
{brief}
---

Return ONLY the JSON object. No preamble, no markdown."""


# ---------------------------------------------------------------------------
# Core DAG generation
# ---------------------------------------------------------------------------

async def generate_dag(
    job_id: str,
    db: AsyncSession,
    model: str | None = None,
) -> dict:
    """Generate a DAG from a job's refined brief and persist nodes.

    Returns dict with job_id, strategy, task_count, tasks, edges, validation.
    """
    uid = UUID(job_id)

    # 1. Fetch job and its refined brief
    result = await db.execute(
        text("SELECT status, refined_brief FROM jobs WHERE id = :id"),
        {"id": uid},
    )
    row = result.first()
    if not row:
        return {"error": f"Job {job_id} not found"}

    status, brief = row
    if status != "planning":
        return {"error": f"Job is in '{status}' state, expected 'planning'"}
    if not brief:
        return {"error": "Job has no refined_brief — run idea refinement first"}

    brief_data = brief if isinstance(brief, dict) else json.loads(brief)

    # 2. Call LLM for decomposition
    prompt = DAG_PROMPT.format(brief=json.dumps(brief_data, indent=2))
    resp = await model_router.generate(
        prompt,
        model=model or model_router.settings.model_general,
        system=DAG_SYSTEM,
        temperature=0.3,
        max_tokens=4096,
    )

    if not resp.success:
        await _fail_job(db, uid, f"LLM DAG generation failed: {resp.error}")
        return {"job_id": job_id, "status": "failed", "error": resp.error}

    # 3. Parse LLM output
    dag_data = _parse_json(resp.text)
    if dag_data is None:
        await _fail_job(db, uid, "Failed to parse DAG JSON from LLM output")
        return {
            "job_id": job_id,
            "status": "failed",
            "error": "LLM output was not valid JSON",
            "raw_output": resp.text[:500],
        }

    tasks = dag_data.get("tasks", [])
    if len(tasks) < 2:
        await _fail_job(db, uid, "DAG must have at least 2 tasks")
        return {"job_id": job_id, "status": "failed", "error": "Less than 2 tasks generated"}

    # 4. Normalize and validate tasks
    normalized, errors = _normalize_tasks(tasks)
    if errors:
        await _fail_job(db, uid, f"Task validation errors: {'; '.join(errors)}")
        return {"job_id": job_id, "status": "failed", "errors": errors}

    # 5. Build edges and validate graph
    edges = _build_edges(normalized)
    graph_errors, warnings = _validate_graph(normalized, edges)
    if graph_errors:
        await _fail_job(db, uid, f"Graph validation errors: {'; '.join(graph_errors)}")
        return {"job_id": job_id, "status": "failed", "errors": graph_errors}

    # 6. Infer strategy
    strategy = dag_data.get("strategy", "")
    if strategy not in VALID_STRATEGIES:
        strategy = _infer_strategy(normalized)

    # 7. Persist DAG nodes to database
    for i, task in enumerate(normalized):
        await db.execute(
            text("""
                INSERT INTO dag_nodes
                    (job_id, node_key, title, node_type, status,
                     depends_on, assigned_model, prompt_template,
                     execution_order, tool)
                VALUES
                    (:job_id, :node_key, :title, :node_type, 'pending',
                     :depends_on, :assigned_model, :prompt_template,
                     :execution_order, :tool)
            """),
            {
                "job_id": uid,
                "node_key": task["id"],
                "title": task["name"],
                "node_type": _map_node_type(task["type"]),
                "depends_on": task.get("depends_on", []),
                "assigned_model": task.get("assigned_model"),
                "prompt_template": task.get("notes"),
                "execution_order": i,
                "tool": task.get("tool", "LLM"),
            },
        )

    # 8. Transition job to executing
    await db.execute(
        text("UPDATE jobs SET status = 'executing' WHERE id = :id"),
        {"id": uid},
    )
    await db.commit()
    logger.info("Job %s: DAG generated with %d nodes → executing", job_id, len(normalized))

    # 9. Generate Mermaid diagram
    mermaid = _render_mermaid(normalized, edges)

    return {
        "job_id": job_id,
        "status": "executing",
        "strategy": strategy,
        "task_count": len(normalized),
        "tasks": normalized,
        "edges": edges,
        "warnings": warnings,
        "mermaid_dag": mermaid,
        "model_used": resp.model,
        "duration_ms": resp.total_duration_ms,
    }


# ---------------------------------------------------------------------------
# Task normalization (from WA tool logic)
# ---------------------------------------------------------------------------

def _normalize_tasks(tasks: list[dict]) -> tuple[list[dict], list[str]]:
    """Normalize and validate task list. Returns (tasks, errors)."""
    errors: list[str] = []
    normalized: list[dict] = []
    seen_ids: set[str] = set()

    for i, raw in enumerate(tasks):
        if not isinstance(raw, dict):
            errors.append(f"Task {i}: must be an object")
            continue

        task_id = str(raw.get("id", "")).strip()
        name = str(raw.get("name", "")).strip()
        task_type = str(raw.get("type", "")).strip()

        if not task_id:
            errors.append(f"Task {i}: missing 'id'")
            continue
        if not name:
            errors.append(f"Task {i}: missing 'name'")
        if task_type not in VALID_TASK_TYPES:
            logger.warning("Task %s: unknown type '%s', coercing to 'action'", task_id, task_type)
            task_type = "action"
        if task_id in seen_ids:
            errors.append(f"Task {i}: duplicate id '{task_id}'")
            continue

        seen_ids.add(task_id)

        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []

        task = {
            "id": task_id,
            "name": name,
            "type": task_type,
            "inputs": raw.get("inputs", []) if isinstance(raw.get("inputs"), list) else [],
            "outputs": raw.get("outputs", []) if isinstance(raw.get("outputs"), list) else [],
            "depends_on": [str(d).strip() for d in depends_on if str(d).strip()],
            "tool": str(raw.get("tool", "LLM")).strip(),
        }
        if task["tool"] not in VALID_TOOLS:
            logger.warning("Task %s: unknown tool '%s', coercing to 'LLM'", task_id, task["tool"])
            task["tool"] = "LLM"
        raw_model = str(raw.get("assigned_model", "")).strip()
        if raw_model and raw_model.lower() not in ("none", "null", ""):
            task["assigned_model"] = raw_model
        elif task.get("tool") == "CodeGen":
            task["assigned_model"] = "qwen2.5-coder:7b"
        if raw.get("notes"):
            task["notes"] = str(raw["notes"]).strip()

        normalized.append(task)

    # Validate dependency references
    valid_ids = {t["id"] for t in normalized}
    for task in normalized:
        for dep in task["depends_on"]:
            if dep not in valid_ids:
                errors.append(f"Task {task['id']}: depends_on references unknown '{dep}'")

    return normalized, errors


# ---------------------------------------------------------------------------
# Graph validation (cycle detection via Kahn's algorithm)
# ---------------------------------------------------------------------------

def _build_edges(tasks: list[dict]) -> list[dict]:
    """Build edge list from task dependencies."""
    edges = []
    for task in tasks:
        for dep in task.get("depends_on", []):
            edges.append({"from": dep, "to": task["id"]})
    return edges


def _validate_graph(tasks: list[dict], edges: list[dict]) -> tuple[list[str], list[str]]:
    """Validate DAG structure using Kahn's algorithm. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    ids = [t["id"] for t in tasks]
    id_set = set(ids)

    # Build adjacency and in-degree
    in_degree: dict[str, int] = {tid: 0 for tid in ids}
    adjacency: dict[str, list[str]] = {tid: [] for tid in ids}
    for edge in edges:
        src, tgt = edge["from"], edge["to"]
        if src in adjacency and tgt in id_set:
            adjacency[src].append(tgt)
            in_degree[tgt] = in_degree.get(tgt, 0) + 1

    # Kahn's algorithm for cycle detection
    queue: deque[str] = deque()
    for tid in ids:
        if in_degree[tid] == 0:
            queue.append(tid)

    sorted_count = 0
    while queue:
        node = queue.popleft()
        sorted_count += 1
        for neighbor in adjacency[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if sorted_count != len(ids):
        errors.append("Circular dependency detected — DAG contains a cycle")

    # Check for roots and leaves
    sources = {e["from"] for e in edges}
    targets = {e["to"] for e in edges}
    roots = id_set - targets
    leaves = id_set - sources

    if not roots and len(ids) > 1:
        errors.append("No root node found (every task has a dependency)")

    # Check for disconnected nodes
    connected = sources | targets
    for tid in ids:
        if tid not in connected and len(ids) > 1:
            warnings.append(f"Task '{tid}' is disconnected from the graph")

    return errors, warnings


# ---------------------------------------------------------------------------
# Strategy inference (from WA tool logic)
# ---------------------------------------------------------------------------

def _infer_strategy(tasks: list[dict]) -> str:
    """Infer decomposition strategy from task structure."""
    task_map = {t["id"]: t for t in tasks}

    if any(t.get("type") == "decision" for t in tasks):
        return "conditional"

    parent_counts: dict[str, int] = {}
    has_join = False
    for task in tasks:
        deps = task.get("depends_on", [])
        if len(deps) > 1:
            has_join = True
        for dep in deps:
            parent_counts[dep] = parent_counts.get(dep, 0) + 1

    has_branch = any(c > 1 for c in parent_counts.values())

    if has_branch and has_join:
        return "hybrid"
    if has_branch:
        return "parallel"
    return "sequential"


# ---------------------------------------------------------------------------
# Mermaid rendering
# ---------------------------------------------------------------------------

def _render_mermaid(tasks: list[dict], edges: list[dict]) -> str:
    """Generate Mermaid flowchart from tasks and edges."""
    if len(tasks) <= 2:
        return ""

    lines = ["flowchart TD"]
    names = {t["id"]: t["name"] for t in tasks}
    for edge in edges:
        src, tgt = edge["from"], edge["to"]
        src_label = _safe_label(names.get(src, src))
        tgt_label = _safe_label(names.get(tgt, tgt))
        lines.append(f"  {src}[{src_label}] --> {tgt}[{tgt_label}]")
    return "\n".join(lines)


def _safe_label(value: str) -> str:
    return value.replace("[", "(").replace("]", ")")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _map_node_type(task_type: str) -> str:
    """Map WA task types to dag_nodes.node_type enum."""
    mapping = {
        "research": "task",
        "decision": "decision",
        "action": "task",
        "validation": "checkpoint",
        "output": "task",
        "human_review": "checkpoint",
    }
    return mapping.get(task_type, "task")


def _parse_json(raw: str) -> dict | None:
    """Extract JSON from LLM output."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass
    return None


async def _fail_job(db: AsyncSession, job_id: UUID, error: str) -> None:
    await db.execute(
        text("""
            UPDATE jobs SET status = 'failed', error_summary = :error
            WHERE id = :id
        """),
        {"error": error[:1000], "id": job_id},
    )
    await db.commit()
    logger.error("Job %s DAG generation failed: %s", job_id, error)
