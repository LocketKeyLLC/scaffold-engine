"""Patch: Task 2 — Idempotency guard on POST /dag (generate_dag).

Target: ~/scaffold-engine/app/modules/dag_generator.py

Changes:
  1. Before the LLM call, checks for existing dag_nodes for the job_id
  2. Returns HTTP 409-equivalent dict if nodes already exist or job not in 'planning'
  Both checks happen before the expensive LLM call (177-226s).
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

# ── Insert idempotency checks after job fetch, before LLM call ──

src = src.replace(
    """    status, brief = row
    if status != "planning":
        return {"error": f"Job is in '{status}' state, expected 'planning'"}
    if not brief:
        return {"error": "Job has no refined_brief — run idea refinement first"}

    brief_data = brief if isinstance(brief, dict) else json.loads(brief)

    # 2. Call LLM for decomposition""",

    """    status, brief = row
    if status != "planning":
        return {
            "error": "Job is not in planning status",
            "job_id": job_id,
            "current_status": status,
            "http_status": 409,
        }
    if not brief:
        return {"error": "Job has no refined_brief — run idea refinement first"}

    # 1b. Idempotency guard — reject if DAG already exists
    existing = await db.execute(
        text("SELECT COUNT(*) FROM dag_nodes WHERE job_id = :jid"),
        {"jid": uid},
    )
    node_count = existing.scalar() or 0
    if node_count > 0:
        logger.warning(
            "idempotency_rejected: job=%s existing_nodes=%d", job_id, node_count
        )
        return {
            "error": "DAG already exists for this job",
            "job_id": job_id,
            "node_count": node_count,
            "http_status": 409,
        }

    brief_data = brief if isinstance(brief, dict) else json.loads(brief)

    # 2. Call LLM for decomposition"""
)

TARGET.write_text(src)
print(f"✅ Task 2 patch applied to {TARGET}")
