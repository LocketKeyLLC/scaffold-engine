"""Patch: Task 1 — Dependency reference validation via standalone validate_dag().

Target: ~/scaffold-engine/app/modules/dag_generator.py

Changes:
  1. Adds validate_dag(nodes) -> (cleaned_nodes, warnings) function
  2. Removes inline dep-reference error from _normalize_tasks (now handled by validate_dag)
  3. Replaces _validate_graph call in generate_dag with validate_dag + _validate_graph combo
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

# ── Patch A: Remove the dep-reference error block from _normalize_tasks ──
# This validation moves into validate_dag where it strips instead of erroring.

src = src.replace(
    """    # Validate dependency references
    valid_ids = {t["id"] for t in normalized}
    for task in normalized:
        for dep in task["depends_on"]:
            if dep not in valid_ids:
                errors.append(f"Task {task['id']}: depends_on references unknown '{dep}'")

    return normalized, errors""",

    """    return normalized, errors"""
)

# ── Patch B: Insert validate_dag function before _build_edges ──

src = src.replace(
    """# ---------------------------------------------------------------------------
# Graph validation (cycle detection via Kahn's algorithm)
# ---------------------------------------------------------------------------""",

    """# ---------------------------------------------------------------------------
# DAG semantic validation (standalone, unit-testable)
# ---------------------------------------------------------------------------

def validate_dag(nodes: list[dict]) -> tuple[list[dict], list[str]]:
    \"\"\"Validate and clean a parsed DAG node list.

    Performs:
      - Dependency reference validation (strips invalid refs)
      - Self-reference removal
      - Tool validation (defaults invalid tools to 'LLM')
      - Cycle detection via topological sort

    Returns (cleaned_nodes, warnings). Raises ValueError on cycles.
    \"\"\"
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
# ---------------------------------------------------------------------------"""
)

# ── Patch C: Wire validate_dag into generate_dag between normalize and graph ──

src = src.replace(
    """    # 4. Normalize and validate tasks
    normalized, errors = _normalize_tasks(tasks)
    if errors:
        await _fail_job(db, uid, f"Task validation errors: {'; '.join(errors)}")
        return {"job_id": job_id, "status": "failed", "errors": errors}

    # 5. Build edges and validate graph
    edges = _build_edges(normalized)
    graph_errors, warnings = _validate_graph(normalized, edges)
    if graph_errors:
        await _fail_job(db, uid, f"Graph validation errors: {'; '.join(graph_errors)}")
        return {"job_id": job_id, "status": "failed", "errors": graph_errors}""",

    """    # 4. Normalize and validate tasks
    normalized, errors = _normalize_tasks(tasks)
    if errors:
        await _fail_job(db, uid, f"Task validation errors: {'; '.join(errors)}")
        return {"job_id": job_id, "status": "failed", "errors": errors}

    # 4b. Semantic DAG validation (deps, cycles, tools)
    try:
        normalized, dag_warnings = validate_dag(normalized)
    except ValueError as exc:
        await _fail_job(db, uid, str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}

    # 5. Build edges and validate graph
    edges = _build_edges(normalized)
    graph_errors, warnings = _validate_graph(normalized, edges)
    warnings.extend(dag_warnings)
    if graph_errors:
        await _fail_job(db, uid, f"Graph validation errors: {'; '.join(graph_errors)}")
        return {"job_id": job_id, "status": "failed", "errors": graph_errors}"""
)

TARGET.write_text(src)
print(f"✅ Task 1 patch applied to {TARGET}")
