"""
Patch 4: Structured partial compile
Replaces [PARTIAL] text prefix with compile_status + failed_nodes in SSE events.
"""
import sys

FILE = "/home/aedefruscio/scaffold-engine/app/modules/execution_agent.py"

with open(FILE) as f:
    src = f.read()

original = src

# --- 4a. Remove the [PARTIAL] prefix from compiled_output in execute_next_node ---
src = src.replace(
    '''            partial_result = await _compile_output(job_id, db)
            if partial_result:
                partial_result = "[PARTIAL — some nodes failed or blocked]\\n\\n" + partial_result
                await db.execute(''',
    '''            partial_result = await _compile_output(job_id, db)
            if partial_result:
                await db.execute(''',
)

# --- 4b. Add compile_status and failed_nodes to the "complete" pipeline_complete event ---
src = src.replace(
    '''        # -- terminal: all nodes done --
        if status == "complete":
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            passed = sum(1 for r in node_results if r.get("verified"))
            failed = len(node_results) - passed
            summary = {
                "job_id": job_id,
                "total_nodes": len(node_results),
                "passed": passed,
                "failed": failed,
                "duration_ms": elapsed_ms,
                "status": "completed",
            }
            logger.info("pipeline_completed: job=%s total=%s passed=%s failed=%s duration_ms=%s", job_id, len(node_results), passed, failed, elapsed_ms)
            yield _sse("pipeline_complete", summary)
            return''',
    '''        # -- terminal: all nodes done --
        if status == "complete":
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            passed = sum(1 for r in node_results if r.get("verified"))
            failed_count = len(node_results) - passed
            is_partial = failed_count > 0
            failed_node_details = [
                {
                    "node_key": r.get("node_key"),
                    "status": r.get("status", "failed"),
                    "reason": r.get("error") or r.get("verification_reason", "unknown"),
                }
                for r in node_results if not r.get("verified")
            ]
            summary = {
                "job_id": job_id,
                "total_nodes": len(node_results),
                "passed": passed,
                "failed": failed_count,
                "duration_ms": elapsed_ms,
                "status": "completed",
                "compile_status": "partial" if is_partial else "complete",
            }
            if is_partial:
                summary["failed_nodes"] = failed_node_details
            logger.info("pipeline_completed: job=%s total=%s passed=%s failed=%s duration_ms=%s", job_id, len(node_results), passed, failed_count, elapsed_ms)
            yield _sse("pipeline_complete", summary)
            return''',
)

# --- 4c. Add compile_status to the early-exit pipeline_complete event ---
src = src.replace(
    '''        # -- early exit: auto-completion fired on last node --
        if result.get("job_complete"):
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            passed = sum(1 for r in node_results if r.get("verified"))
            failed = len(node_results) - passed
            yield _sse("pipeline_complete", {
                "job_id": job_id,
                "total_nodes": len(node_results),
                "passed": passed,
                "failed": failed,
                "duration_ms": elapsed_ms,
            })
            return''',
    '''        # -- early exit: auto-completion fired on last node --
        if result.get("job_complete"):
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            passed = sum(1 for r in node_results if r.get("verified"))
            failed_count = len(node_results) - passed
            is_partial = failed_count > 0
            failed_node_details = [
                {
                    "node_key": r.get("node_key"),
                    "status": r.get("status", "failed"),
                    "reason": r.get("error") or r.get("verification_reason", "unknown"),
                }
                for r in node_results if not r.get("verified")
            ]
            early_summary = {
                "job_id": job_id,
                "total_nodes": len(node_results),
                "passed": passed,
                "failed": failed_count,
                "duration_ms": elapsed_ms,
                "compile_status": "partial" if is_partial else "complete",
            }
            if is_partial:
                early_summary["failed_nodes"] = failed_node_details
            yield _sse("pipeline_complete", early_summary)
            return''',
)

if src == original:
    print("ERROR: No replacements applied — source text did not match.")
    sys.exit(1)

with open(FILE, "w") as f:
    f.write(src)

print("PATCH 4 APPLIED: Structured partial compile")
print("  - Removed [PARTIAL] text prefix from compiled_output")
print("  - Added compile_status ('complete'|'partial') to pipeline_complete events")
print("  - Added failed_nodes array when partial")
