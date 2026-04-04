"""
Patch 2: Concurrent execution guard
Adds atomic check-and-set at top of execute_all_nodes to prevent TOCTOU races.
"""
import sys

FILE = "/home/aedefruscio/scaffold-engine/app/modules/execution_agent.py"

with open(FILE) as f:
    src = f.read()

original = src

# Insert the guard right after the t0/node_results setup, before job validation.
# The existing code is:
#     t0 = _time.monotonic()
#     node_results: list[dict] = []
#
#     # ---- validate job ----
#     job = await _get_job(db, job_id)

src = src.replace(
    '''    t0 = _time.monotonic()
    node_results: list[dict] = []

    # ---- validate job ----
    job = await _get_job(db, job_id)
    if not job:
        yield _sse("error", {"message": f"Job {job_id} not found"})
        return
    if job["status"] not in ("executing", "planning", "refining"):
        yield _sse("error", {
            "message": f"Job status is '{job['status']}' — not executable",
        })
        return''',
    '''    t0 = _time.monotonic()
    node_results: list[dict] = []

    # ---- concurrent execution guard (atomic check-and-set) ----
    guard_result = await db.execute(
        text("""
            UPDATE jobs SET status = 'running', updated_at = now()
            WHERE id = :jid AND status != 'running'
            RETURNING id
        """),
        {"jid": job_id},
    )
    if guard_result.rowcount == 0:
        # Job is already running or doesn't exist — check which
        job_check = await _get_job(db, job_id)
        if not job_check:
            yield _sse("error", {"message": f"Job {job_id} not found"})
        else:
            yield _sse("error", {
                "message": "Job is already executing",
                "job_id": job_id,
                "http_status": 409,
            })
        return
    await db.commit()

    # ---- validate job ----
    job = await _get_job(db, job_id)
    if not job:
        yield _sse("error", {"message": f"Job {job_id} not found"})
        return
    if job["status"] not in ("running", "executing", "planning", "refining"):
        yield _sse("error", {
            "message": f"Job status is '{job['status']}' — not executable",
        })
        return''',
)

if src == original:
    print("ERROR: No replacements applied — source text did not match.")
    sys.exit(1)

with open(FILE, "w") as f:
    f.write(src)

print("PATCH 2 APPLIED: Concurrent execution guard")
print("  - Atomic UPDATE ... WHERE status != 'running' RETURNING id")
print("  - Returns 409-equivalent SSE error if job already running")
print("  - Allowed 'running' in the status validation check")
