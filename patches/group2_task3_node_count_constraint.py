"""Patch: Task 3 — Node count constraint in decomposition prompt.

Target: ~/scaffold-engine/app/modules/dag_generator.py

Changes:
  1. Updates DAG_SYSTEM prompt: 3-5 steps constraint + one-shot example
  2. Adds _enforce_node_count() to truncate >5 or log <3
  3. Wires enforcement into generate_dag between parse and normalize
"""

import sys
from pathlib import Path

TARGET = Path.home() / "scaffold-engine" / "app" / "modules" / "dag_generator.py"
if not TARGET.exists():
    TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else TARGET
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found. Pass path as arg.")
        sys.exit(1)

src = TARGET.read_text()

# ── Patch A: Replace the DAG_SYSTEM prompt with constrained version ──

src = src.replace(
    '''DAG_SYSTEM = """You are a workflow decomposition engine. Given a structured brief, produce a DAG of executable tasks.

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
- If complexity is high or ambiguities exist, include a human_review task"""''',

    '''DAG_SYSTEM = """You are a workflow decomposition engine. Given a structured brief, produce a DAG of executable tasks.

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
- Decompose the idea into exactly 3 to 5 execution steps. Do not create more than 5 steps. If the task is simple, use 3 steps. If it requires research, retrieval, and synthesis, use 4-5 steps.
- Every task must have a unique id (T1, T2, ...)
- depends_on references other task ids — only use ids you have defined
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
- If complexity is high or ambiguities exist, include a human_review task

EXAMPLE (4-node DAG for "Research the history of solar panels and summarize findings"):
{
  "strategy": "sequential",
  "tasks": [
    {"id": "T1", "name": "Search solar panel history", "type": "research", "inputs": ["solar panel history query"], "outputs": ["raw search results"], "depends_on": [], "tool": "SearXNG", "assigned_model": null, "notes": "Broad web search for timeline and key milestones"},
    {"id": "T2", "name": "Retrieve internal KB context", "type": "research", "inputs": ["solar panel keywords"], "outputs": ["KB matches"], "depends_on": ["T1"], "tool": "Milvus", "assigned_model": null, "notes": "Check knowledge base for any stored solar energy references"},
    {"id": "T3", "name": "Synthesize and summarize", "type": "action", "inputs": ["raw search results", "KB matches"], "outputs": ["summary draft"], "depends_on": ["T1", "T2"], "tool": "LLM", "assigned_model": null, "notes": "Combine sources into a coherent summary"},
    {"id": "T4", "name": "Format final output", "type": "output", "inputs": ["summary draft"], "outputs": ["final summary document"], "depends_on": ["T3"], "tool": "FileSystem", "assigned_model": null, "notes": "Write final summary to file"}
  ]
}"""'''
)

# ── Patch B: Add _enforce_node_count helper before _normalize_tasks ──

src = src.replace(
    """# ---------------------------------------------------------------------------
# Task normalization (from WA tool logic)
# ---------------------------------------------------------------------------""",

    """# ---------------------------------------------------------------------------
# Node count enforcement
# ---------------------------------------------------------------------------

def _enforce_node_count(
    tasks: list[dict], min_count: int = 3, max_count: int = 5
) -> list[dict]:
    \"\"\"Enforce node count bounds. Truncates excess nodes and cleans dangling refs.\"\"\"
    if len(tasks) < min_count:
        logger.warning(
            "dag_undercount: node_count=%d", len(tasks)
        )
        return tasks

    if len(tasks) > max_count:
        # Sort by node_key, keep first max_count
        sorted_tasks = sorted(tasks, key=lambda t: t.get("id", ""))
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
# ---------------------------------------------------------------------------"""
)

# ── Patch C: Wire _enforce_node_count into generate_dag after parse, before normalize ──

src = src.replace(
    """    tasks = dag_data.get("tasks", [])
    if len(tasks) < 2:
        await _fail_job(db, uid, "DAG must have at least 2 tasks")
        return {"job_id": job_id, "status": "failed", "error": "Less than 2 tasks generated"}

    # 4. Normalize and validate tasks""",

    """    tasks = dag_data.get("tasks", [])
    if len(tasks) < 2:
        await _fail_job(db, uid, "DAG must have at least 2 tasks")
        return {"job_id": job_id, "status": "failed", "error": "Less than 2 tasks generated"}

    # 3b. Enforce node count bounds (3-5)
    tasks = _enforce_node_count(tasks)

    # 4. Normalize and validate tasks"""
)

TARGET.write_text(src)
print(f"✅ Task 3 patch applied to {TARGET}")
