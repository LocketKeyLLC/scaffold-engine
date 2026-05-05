"""Scaffold Engine — DAG generator module.

Takes a refined brief (from Step 10) → LLM decomposition → validated DAG.
Reuses Workflow Architect validation logic:
  - Kahn-based cycle detection
  - Strategy inference (sequential/parallel/hybrid/conditional)
  - I/O contract auditing

Persists nodes to dag_nodes table. Job transitions: planning → executing.

Step 11 of 23-step build plan.
"""

import json
import logging
import re
from collections import deque
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router
from app.config import get_model
from app.utils.job_utils import fail_job as _fail_job
from app.utils.llm_parsing import parse_json_object

logger = logging.getLogger("scaffold.dag")

# ---------------------------------------------------------------------------
# Valid enums — imported from config (#101)
# ---------------------------------------------------------------------------

from app.config import (
    VALID_TASK_TYPES,
    VALID_STRATEGIES,
    VALID_TOOLS,
    VALID_DOMAINS,
)

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
      "type": "research | decision | action | validation | output",
      "inputs": ["what this task consumes"],
      "outputs": ["what this task produces"],
      "depends_on": [],
      "tool": "LLM | SearXNG | Milvus | CodeGen",
      "domain": "prompt | rag | eng | llm | spec | null",
      "assigned_model": "model name or null",
      "notes": "optional execution hint"
    }
  ]
}

Rules:
- Decompose the idea into exactly 3 to 10 execution steps. Do not create more than 10 steps. If the task is simple, use 3 steps. If it requires research, retrieval, and synthesis, use 4-10 steps.
- Every task must have a unique id (T1, T2, ...)
- depends_on references other task ids — only use ids you have defined
- No circular dependencies
- First task(s) must have empty depends_on
- Last task(s) must be type "output" or "validation"
- Keep task names to max 5 words
- Tool guide:
  * Milvus = ALWAYS use when the task involves the knowledge base, KB, internal docs, TOON files, or domain-specific lookup. Any mention of "knowledge base", "KB", "look up from", "retrieve from", or stored/internal knowledge MUST use Milvus, NEVER SearXNG.
    - When tool is Milvus, you MUST set "domain" to the most relevant knowledge domain: "prompt" (prompt engineering), "rag" (retrieval-augmented generation), "eng" (software engineering), "llm" (large language models), "spec" (specifications/architecture). If unsure, set "domain" to null.
    - When tool is NOT Milvus, set "domain" to null.
  * SearXNG = web search for EXTERNAL, current, or live information NOT in the knowledge base.
  * CodeGen = the deliverable IS executable code. The node produces a working
    script, function, module, or class as its primary output. The user runs the
    output. Examples: "Write the parser", "Implement the API endpoint",
    "Generate the Dockerfile". Do NOT use CodeGen for: listing file extensions,
    naming variables, designing schemas, choosing libraries, writing
    documentation, listing requirements, or describing what code should do.
    If the deliverable is a list, plan, decision, design doc, or explanation —
    even one ABOUT code — use LLM, not CodeGen.
  * LLM = general reasoning, summarization, analysis, planning, listing,
    decision-making, design, explanation, and documentation. This is the
    DEFAULT — use it whenever the deliverable is text rather than executable
    code.
- Each node must produce DISTINCT output that no other node produces. Do NOT create multiple nodes that generate the same artifact (e.g., do not have separate "design script" and "write script" nodes that both produce the full script).
- Later nodes must EXTEND or VALIDATE earlier work, never recreate it. For example: T1 writes the code → T2 writes tests for it → T3 validates both — NOT T1 designs code → T2 rewrites the same code → T3 rewrites it again.
- If a task can be accomplished in one node, use one node. Prefer fewer, focused nodes over many overlapping ones.

EXAMPLE (4-node DAG for "Research the history of solar panels and summarize findings"):
{
  "strategy": "sequential",
  "tasks": [
    {"id": "T1", "name": "Search solar panel history", "type": "research", "inputs": ["solar panel history query"], "outputs": ["raw search results"], "depends_on": [], "tool": "SearXNG", "domain": null, "assigned_model": null, "notes": "Broad web search for timeline and key milestones"},
    {"id": "T2", "name": "Retrieve internal KB context", "type": "research", "inputs": ["solar panel keywords"], "outputs": ["KB matches"], "depends_on": ["T1"], "tool": "Milvus", "domain": "eng", "assigned_model": null, "notes": "Check knowledge base for any stored solar energy references"},
    {"id": "T3", "name": "Synthesize and summarize", "type": "action", "inputs": ["raw search results", "KB matches"], "outputs": ["summary draft"], "depends_on": ["T1", "T2"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Combine sources into a coherent summary"},
    {"id": "T4", "name": "Format final output", "type": "output", "inputs": ["summary draft"], "outputs": ["final summary document"], "depends_on": ["T3"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Write final summary to file"}
  ]
}

EXAMPLE (5-node DAG for "Build a CLI tool that converts screenshots to a searchable PDF"):
{
  "strategy": "sequential",
  "tasks": [
    {"id": "T1", "name": "Decide library stack", "type": "decision", "inputs": ["project goals"], "outputs": ["chosen libraries and why"], "depends_on": [], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Pick OCR + PDF libs (e.g., pytesseract, pypdf) — text decision, NOT code"},
    {"id": "T2", "name": "List supported file types", "type": "decision", "inputs": ["chosen libraries"], "outputs": ["list of extensions"], "depends_on": ["T1"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Plain list of extensions like .png .jpg — LLM not CodeGen"},
    {"id": "T3", "name": "Write the CLI script", "type": "action", "inputs": ["library stack", "file types"], "outputs": ["working Python script"], "depends_on": ["T1", "T2"], "tool": "CodeGen", "domain": null, "assigned_model": null, "notes": "Real code is the deliverable — CodeGen"},
    {"id": "T4", "name": "Document usage", "type": "action", "inputs": ["working Python script"], "outputs": ["README content"], "depends_on": ["T3"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Documentation about code is LLM, not CodeGen"},
    {"id": "T5", "name": "Validate end-to-end", "type": "validation", "inputs": ["working Python script", "README content"], "outputs": ["validation report"], "depends_on": ["T3", "T4"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Validation is reasoning, not code"}
  ]
}"""

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
    model_overrides: dict | None = None,
) -> dict:
    """Generate a DAG from a job's refined brief and persist nodes.

    Returns dict with job_id, strategy, task_count, tasks, edges, validation.
    """
    uid = UUID(job_id)

    # 1. Fetch job + refined brief + existing dag_nodes count under a row lock.
    # FOR UPDATE serializes concurrent /dag calls on the same job_id so the
    # status + node-count checks below are race-free with the INSERT block.
    # Lock is released on commit/rollback at the end of this transaction.
    result = await db.execute(
        text("""
            SELECT j.status, j.refined_brief,
                   (SELECT COUNT(*) FROM dag_nodes WHERE job_id = j.id) AS node_count
            FROM jobs j
            WHERE j.id = :id
            FOR UPDATE OF j
        """),
        {"id": uid},
    )
    row = result.first()
    if not row:
        await db.rollback()
        return {"error": f"Job {job_id} not found"}

    status, brief, node_count = row
    # H6: accept 'planning' OR 'running' — execute_all_nodes flips to 'running'
    # before calling generate_dag on auto-gen path.
    if status not in ("planning", "running"):
        await db.rollback()
        return {
            "error": "Job is not in an executable planning/running status",
            "job_id": job_id,
            "current_status": status,
            "http_status": 409,
        }
    if not brief:
        await db.rollback()
        return {"error": "Job has no refined_brief — run idea refinement first"}

    if (node_count or 0) > 0:
        logger.warning(
            "idempotency_rejected: job=%s existing_nodes=%d", job_id, node_count
        )
        await db.rollback()
        return {
            "error": "DAG already exists for this job",
            "job_id": job_id,
            "node_count": node_count,
            "http_status": 409,
        }

    brief_data = brief if isinstance(brief, dict) else json.loads(brief)

    # 2. Call LLM for decomposition
    prompt = DAG_PROMPT.format(brief=json.dumps(brief_data, indent=2))
    resp = await model_router.generate(
        prompt,
        model=model or get_model("model_general", model_overrides),
        system=DAG_SYSTEM,
        temperature=0.3,
        max_tokens=4096,
    )

    if not resp.success:
        await _fail_job(db, uid, f"LLM DAG generation failed: {resp.error}")
        return {"job_id": job_id, "status": "failed", "error": resp.error}

    # 3. Parse LLM output
    dag_data = parse_json_object(resp.text)
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

    # 3b-4b. Node-count enforcement + normalize + semantic validation
    # (single try/except so any ValueError from these steps fails the job cleanly)
    try:
        tasks = _enforce_node_count(tasks)
        normalized, errors, normalize_warnings = _normalize_tasks(tasks)
        if errors:
            await _fail_job(db, uid, f"Task validation errors: {'; '.join(errors)}")
            return {"job_id": job_id, "status": "failed", "errors": errors}
        normalized, dag_warnings = validate_dag(normalized)
    except ValueError as exc:
        await _fail_job(db, uid, str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}

    # 5. Build edges and validate graph
    edges = _build_edges(normalized)
    graph_errors, warnings = _validate_graph(normalized, edges)
    warnings.extend(dag_warnings)
    warnings.extend(normalize_warnings)  # #26 #25
    if graph_errors:
        await _fail_job(db, uid, f"Graph validation errors: {'; '.join(graph_errors)}")
        return {"job_id": job_id, "status": "failed", "errors": graph_errors}

    # 6. Infer strategy
    strategy = dag_data.get("strategy", "")
    if strategy not in VALID_STRATEGIES:
        strategy = _infer_strategy(normalized)

    # 7. Persist DAG nodes to database
    # 6b. Compute leaf set (#97): node is a leaf if nothing depends on it.
    referenced = set()
    for t in normalized:
        for dep in t.get("depends_on", []) or []:
            referenced.add(dep)
    leaf_keys = {t["id"] for t in normalized if t["id"] not in referenced}

    try:
        for i, task in enumerate(normalized):
            await db.execute(
                text("""
                    INSERT INTO dag_nodes
                        (job_id, node_key, title, node_type, status,
                         depends_on, assigned_model, prompt_template,
                         execution_order, tool, domain, is_output_node)
                    VALUES
                        (:job_id, :node_key, :title, :node_type, 'pending',
                         :depends_on, :assigned_model, :prompt_template,
                         :execution_order, :tool, :domain, :is_output_node)
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
                    "domain": task.get("domain"),
                    "is_output_node": task["id"] in leaf_keys,
                },
            )

        # 8. Transition job to executing
        await db.execute(
            text("UPDATE jobs SET status = 'executing' WHERE id = :id"),
            {"id": uid},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("dag_insert_failed: job=%s", job_id)
        await _fail_job(db, uid, f"DAG persistence failed: {exc}")
        return {"job_id": job_id, "status": "failed", "error": f"DAG persistence failed: {exc}"}
    logger.info("dag_generated: job=%s node_count=%d", job_id, len(normalized))

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
# Node count enforcement
# ---------------------------------------------------------------------------

def _enforce_node_count(
    tasks: list[dict], min_count: int = 3, max_count: int = 10
) -> list[dict]:
    """Enforce node count bounds. Truncates excess nodes and cleans dangling refs."""
    if len(tasks) < min_count:
        # #23: undercount is a hard failure now. generate_dag catches ValueError
        # at the validate_dag boundary and rolls the job to failed via _fail_job.
        raise ValueError(
            f"dag_undercount: got {len(tasks)} tasks, required minimum {min_count}"
        )

    if len(tasks) > max_count:
        # Sort by node_key, keep first max_count
        sorted_tasks = sorted(tasks, key=lambda t: int(re.sub(r"\D", "", t.get("id", "0")) or "0"))
        kept = sorted_tasks[:max_count]
        dropped = sorted_tasks[max_count:]
        dropped_keys = {t["id"] for t in dropped}
        kept_keys = {t["id"] for t in kept}

        # Rewrite depends_on to remove references to dropped nodes
        for task in kept:
            task["depends_on"] = [
                d for d in task.get("depends_on", []) if d in kept_keys
            ]

        logger.warning(
            "dag_truncated: original_count=%d kept_count=%d dropped_keys=%s",
            len(tasks), max_count, sorted(dropped_keys),
        )
        return kept

    return tasks


# ---------------------------------------------------------------------------
# Task normalization (from WA tool logic)
# ---------------------------------------------------------------------------

def _normalize_tasks(tasks: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Normalize and validate task list. Returns (tasks, errors, warnings).

    #26: warnings list surfaces silent coercions (unknown type/tool defaulting)
    and #25 Milvus-without-domain to the caller instead of log-only.
    """
    errors: list[str] = []
    warnings: list[str] = []
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
            continue  # #99
        if len(name.split()) > 5:  # #104
            errors.append(f"Task {i}: name exceeds 5 words: '{name}'")
            continue
        if task_type not in VALID_TASK_TYPES:
            msg = f"Task {task_id}: unknown type '{task_type}', coercing to 'action'"  # #26
            logger.warning(msg)
            warnings.append(msg)
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
        # Preserve domain for Milvus nodes (validated against VALID_DOMAINS)
        # #108: compute once, reuse for gate + validity check
        raw_domain = raw.get("domain")
        domain_val = str(raw_domain).strip().lower() if raw_domain else ""
        if domain_val and domain_val not in ("none", "null"):
            if domain_val in VALID_DOMAINS:
                task["domain"] = domain_val
            else:
                logger.warning(
                    "invalid_domain_defaulted: node_key=%s original_domain=%s",
                    task_id, raw_domain,
                )
        if task["tool"] not in VALID_TOOLS:
            msg = f"Task {task_id}: unknown tool '{task['tool']}', coercing to 'LLM'"  # #26
            logger.warning(msg)
            warnings.append(msg)
            task["tool"] = "LLM"
        raw_model = str(raw.get("assigned_model", "")).strip()
        if raw_model and raw_model.lower() not in ("none", "null", ""):
            task["assigned_model"] = raw_model
        elif task.get("tool") == "CodeGen":
            task["assigned_model"] = "qwen2.5-coder:7b"
        if raw.get("notes"):
            task["notes"] = str(raw["notes"]).strip()

        # #25: Milvus nodes require a domain; warn if missing or dropped as invalid
        if task.get("tool") == "Milvus" and not task.get("domain"):
            msg = f"Task {task_id}: Milvus tool requires 'domain' field; none set"
            logger.warning(msg)
            warnings.append(msg)

        normalized.append(task)

    return normalized, errors, warnings


# ---------------------------------------------------------------------------
# DAG semantic validation (standalone, unit-testable)
# ---------------------------------------------------------------------------

def validate_dag(nodes: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate and clean a parsed DAG node list.

    Performs:
      - Dependency reference validation (strips invalid refs)
      - Self-reference removal
      - Tool validation (defaults invalid tools to 'LLM')
      - Cycle detection via topological sort

    Returns (cleaned_nodes, warnings). Raises ValueError on cycles.
    """
    warnings: list[str] = []
    valid_keys = {n["id"] for n in nodes}

    for node in nodes:
        nk = node["id"]

        # ── Tool validation ──
        if node.get("tool") not in VALID_TOOLS:
            original = node.get("tool")
            node["tool"] = "LLM"
            msg = f"invalid_tool_defaulted: node_key={nk} original_tool={original} defaulted_to=LLM"
            logger.warning(msg)
            warnings.append(msg)

        # ── Self-reference removal ──
        if nk in node.get("depends_on", []):
            node["depends_on"] = [d for d in node["depends_on"] if d != nk]
            msg = f"self_reference_removed: node_key={nk}"
            logger.warning(msg)
            warnings.append(msg)

        # ── Invalid dependency removal ──
        cleaned_deps: list[str] = []
        for dep in node.get("depends_on", []):
            if dep in valid_keys:
                cleaned_deps.append(dep)
            else:
                msg = (
                    f"invalid_dependency: node_key={nk} "
                    f"invalid_ref={dep} valid_keys={sorted(valid_keys)}"
                )
                logger.warning(msg)
                warnings.append(msg)
        node["depends_on"] = cleaned_deps

    # ── Cycle detection (Kahn's topological sort) ──
    in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
    adjacency: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for node in nodes:
        for dep in node["depends_on"]:
            adjacency[dep].append(node["id"])
            in_degree[node["id"]] += 1

    queue: deque[str] = deque(k for k, v in in_degree.items() if v == 0)
    sorted_count = 0
    while queue:
        cur = queue.popleft()
        sorted_count += 1
        for neighbor in adjacency[cur]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if sorted_count != len(nodes):
        cycle_nodes = [k for k, v in in_degree.items() if v > 0]
        msg = f"dag_cycle_detected: involved_keys={cycle_nodes}"
        logger.error(msg)
        raise ValueError(msg)

    return nodes, warnings


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
    """Validate DAG structure (roots, leaves, connectivity). Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    ids = [t["id"] for t in tasks]
    id_set = set(ids)


    # Check for roots and leaves
    sources = {e["from"] for e in edges}
    targets = {e["to"] for e in edges}
    roots = id_set - targets
    # #27: removed dead `leaves = id_set - sources`

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
    """Generate Mermaid flowchart from tasks and edges.

    #102: render even 1-2 task DAGs; Mermaid handles single nodes fine.
    Empty task list still returns empty string to avoid malformed output.
    """
    if not tasks:
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
    # #28: escape all Mermaid-breaking chars, not just square brackets
    replacements = [
        ("[", "("), ("]", ")"),
        ("{", "("), ("}", ")"),
        ("|", "/"), ('"', "'"),
        ("#", "No."),
    ]
    for old, new in replacements:
        value = value.replace(old, new)
    return value


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
    }
    return mapping.get(task_type, "task")



