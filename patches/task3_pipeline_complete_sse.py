"""
Patch: Emit pipeline_complete SSE event in execute_all_nodes()

Apply: python3 patches/task3_pipeline_complete_sse.py
Target: app/modules/execution_agent.py

Problem:
  After _compile_output() fires (remaining == 0), the while loop continues.
  _get_next_node() discovers job status is 'completed' and emits an error event.
  scaffold_router.py never receives 'pipeline_complete' — falls back to polling.

Fix:
  After the auto-completion block, yield 'pipeline_complete' SSE event with
  compiled_output + compile_status + failed_nodes, then break the loop.

  This patch targets TWO code paths:
  1. Normal completion (all nodes done, remaining == 0)
  2. Early exit (all remaining nodes blocked/failed)

  Both paths already construct compile_status from 4.12 Task 4 — we just need
  to yield the SSE event and break instead of falling through.
"""

import pathlib
import sys

TARGET = pathlib.Path(__file__).resolve().parent.parent / "app" / "modules" / "execution_agent.py"


def apply():
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found")
        sys.exit(1)

    code = TARGET.read_text()
    original = code

    # ──────────────────────────────────────────────────────────────────
    # PATCH 1: Normal completion path (remaining == 0 after node_done)
    #
    # FIND the block where _compile_output is called after remaining == 0.
    # The current code continues the loop; we need to yield + break.
    #
    # We target the pattern AFTER _compile_output() and job status update,
    # right before the loop would continue to _get_next_node().
    # ──────────────────────────────────────────────────────────────────

    # Look for the auto-completion block. Based on the carryover docs,
    # there's a check like:  if remaining.scalar() == 0:
    # followed by _compile_output() and status update to 'completed'.
    #
    # After that block, the code falls through to the next loop iteration.
    # We need to add a yield + break RIGHT AFTER the status update.

    # Strategy: find the compile_status assignment in the normal completion
    # path and add the SSE yield + break after it.
    #
    # From 4.12 Task 4: compile_status = "complete" is set in normal path,
    # compile_status = "partial" in early exit path.

    # --- PATCH 1a: Normal completion ---
    # Target: the line after compile_status is set to "complete" in the
    # auto-completion block (remaining == 0).
    #
    # We search for the pattern where compiled output is fetched after
    # _compile_output() runs, then inject the SSE yield.

    # Since we can't know exact code, we use a flexible approach:
    # Find 'compile_status = "complete"' and inject after the enclosing block.

    patch1_marker = 'compile_status = "complete"'
    if patch1_marker not in code:
        print(f"WARNING: Could not find '{patch1_marker}' — trying alternate pattern")
        patch1_marker = "compile_status = 'complete'"

    if patch1_marker not in code:
        print("ERROR: Cannot locate normal completion path. Manual patch needed.")
        print("See MANUAL PATCH INSTRUCTIONS below.")
        _print_manual_instructions()
        sys.exit(1)

    # --- PATCH 1b: Early exit / partial completion ---
    patch2_marker = 'compile_status = "partial"'
    if patch2_marker not in code:
        patch2_marker = "compile_status = 'partial'"

    if patch2_marker not in code:
        print("WARNING: Cannot locate partial completion path — only patching normal path")

    # For safety, rather than blind injection, print the manual patch
    # instructions with exact code blocks the user should insert.
    _print_manual_instructions()
    print("\n" + "=" * 60)
    print("Attempting automated patch...")
    print("=" * 60)

    # Automated patch: wrap the compile_status assignments to yield SSE
    # We'll inject a helper that both paths call.

    # First, add the helper function before execute_all_nodes
    helper_code = '''

async def _build_pipeline_complete_event(job_id, db, compile_status, failed_nodes=None):
    """Build the pipeline_complete SSE event payload."""
    result = await db.execute(
        text("SELECT compiled_output FROM jobs WHERE id = :jid"),
        {"jid": job_id},
    )
    row = result.first()
    compiled = row.compiled_output if row else None

    payload = {
        "job_id": job_id,
        "compiled_output": compiled,
        "compile_status": compile_status,
    }
    if failed_nodes:
        payload["failed_nodes"] = failed_nodes

    return payload

'''

    # Insert helper before execute_all_nodes definition
    if "_build_pipeline_complete_event" not in code:
        anchor = "async def execute_all_nodes"
        if anchor in code:
            code = code.replace(anchor, helper_code + anchor, 1)
            print("✓ Inserted _build_pipeline_complete_event() helper")
        else:
            print("ERROR: Cannot find 'async def execute_all_nodes'")
            sys.exit(1)
    else:
        print("⏭  _build_pipeline_complete_event() already exists")

    # Now we need to find the two completion paths and add yield + break.
    # This is the trickiest part without seeing exact code.
    #
    # STRATEGY: We know that after compile_status is set, the code should
    # yield the event and break. We'll look for compile_status assignment
    # and check if there's already a yield after it.

    if "pipeline_complete" not in code or 'event: pipeline_complete' not in code:
        # Need to add the SSE yield. We'll target each compile_status assignment.
        # Since code structure varies, print manual instructions as primary path.
        print("\n⚠  Automated yield injection skipped — apply manually (see above)")
        print("   The helper function has been added. You need to add the yield + break.")
    else:
        print("⏭  pipeline_complete SSE event already present in code")

    if code != original:
        TARGET.write_text(code)
        print(f"\n✓ Patched {TARGET}")
        print("  Review the file and add the yield + break at both completion paths.")
    else:
        print("\nNo automated changes applied. Use manual instructions above.")


def _print_manual_instructions():
    print("""
╔══════════════════════════════════════════════════════════════╗
║          MANUAL PATCH — pipeline_complete SSE event          ║
╚══════════════════════════════════════════════════════════════╝

In app/modules/execution_agent.py, make these changes:

━━━ CHANGE 1: Add import (if not present) ━━━━━━━━━━━━━━━━━━━

At the top, ensure you have:
    import json

━━━ CHANGE 2: Normal completion path ━━━━━━━━━━━━━━━━━━━━━━━━

Find the auto-completion block inside execute_all_nodes().
It looks something like:

    # Check if all nodes are done
    remaining = await db.execute(
        text("SELECT COUNT(*) FROM dag_nodes WHERE job_id = :jid AND status NOT IN ('done', 'failed', 'skipped')"),
        {"jid": job_id},
    )
    if remaining.scalar() == 0:
        compiled = await _compile_output(job_id, db)
        await db.execute(
            text("UPDATE jobs SET status = 'completed', compiled_output = :out, updated_at = NOW() WHERE id = :jid"),
            {"jid": job_id, "out": compiled},
        )
        await db.commit()
        compile_status = "complete"
        # ... possibly more code here ...

AFTER the compile_status assignment and any commit, ADD:

        # ── pipeline_complete SSE event (Task #3) ──
        failed_result = await db.execute(
            text("SELECT node_key, status, output_text FROM dag_nodes WHERE job_id = :jid AND status IN ('failed', 'skipped')"),
            {"jid": job_id},
        )
        failed_nodes = [
            {"node_key": r.node_key, "status": r.status, "reason": (r.output_text or "")[:200]}
            for r in failed_result
        ]
        if failed_nodes:
            compile_status = "partial"

        complete_payload = {
            "job_id": job_id,
            "compiled_output": compiled,
            "compile_status": compile_status,
        }
        if failed_nodes:
            complete_payload["failed_nodes"] = failed_nodes

        logger.info(
            "pipeline_complete",
            job_id=job_id,
            compile_status=compile_status,
            total_nodes=total_node_count,
            failed_count=len(failed_nodes),
        )

        yield f"event: pipeline_complete\\ndata: {json.dumps(complete_payload)}\\n\\n"
        return  # Exit the async generator cleanly

━━━ CHANGE 3: Early exit path (all blocked/no actionable nodes) ━━━━━

Find the block where the loop detects no actionable nodes remain
(the "blocked" event path). It probably looks like:

    if node is None:
        # No more actionable nodes
        ...
        yield f"event: blocked\\ndata: ...\\n\\n"
        ...

AFTER the blocked event yield, ADD a check:

        # Check if job is actually done (all nodes resolved)
        still_pending = await db.execute(
            text("SELECT COUNT(*) FROM dag_nodes WHERE job_id = :jid AND status = 'pending'"),
            {"jid": job_id},
        )
        if still_pending.scalar() == 0:
            # All nodes resolved (done/failed/skipped/blocked) — emit pipeline_complete
            compiled = await _compile_output(job_id, db)
            await db.execute(
                text("UPDATE jobs SET status = 'completed', compiled_output = :out, updated_at = NOW() WHERE id = :jid"),
                {"jid": job_id, "out": compiled},
            )
            await db.commit()

            failed_result = await db.execute(
                text("SELECT node_key, status, output_text FROM dag_nodes WHERE job_id = :jid AND status IN ('failed', 'skipped', 'blocked')"),
                {"jid": job_id},
            )
            failed_nodes = [
                {"node_key": r.node_key, "status": r.status, "reason": (r.output_text or "")[:200]}
                for r in failed_result
            ]

            complete_payload = {
                "job_id": job_id,
                "compiled_output": compiled,
                "compile_status": "partial" if failed_nodes else "complete",
            }
            if failed_nodes:
                complete_payload["failed_nodes"] = failed_nodes

            logger.info(
                "pipeline_complete",
                job_id=job_id,
                compile_status=complete_payload["compile_status"],
                failed_count=len(failed_nodes),
            )

            yield f"event: pipeline_complete\\ndata: {json.dumps(complete_payload)}\\n\\n"
            return

        break  # Exit loop — remaining nodes are blocked

━━━ CHANGE 4: Remove cosmetic error ━━━━━━━━━━━━━━━━━━━━━━━━━

The error event that fires when the loop discovers "Job status is
'completed' — not executable" should no longer fire because we now
break/return before reaching that code path. If it still fires,
find the _get_next_node() call that triggers it and add a guard:

    if job_status == 'completed':
        return  # Already handled by pipeline_complete yield above

━━━ DONE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

After applying, verify with:
    docker compose up -d --build scaffold-orchestrator
    docker exec scaffold-orchestrator pytest tests/test_pipeline_complete.py tests/test_status_logs.py -v
""")


if __name__ == "__main__":
    apply()
