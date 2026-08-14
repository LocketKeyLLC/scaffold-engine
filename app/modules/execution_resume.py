"""§17.774 — automatic crash-resume of orphaned mid-execution jobs.

Node-level state already survives a crash: ``dag_nodes.status`` and
``output_text`` are durable, ``execute_all_nodes`` is idempotent over ``done``
nodes (it reuses their output as upstream context), and the lifespan sweep
(``app.main._pre_migration_sweep``) resets any orphaned ``running`` node back to
``pending``. What was missing is the *re-launch*: after a process crash
(SIGKILL / OOM / power loss) the parent job stays ``running`` and nothing
re-drives it, so a 45-minute run stalls until the ~26 h reaper fails it and the
operator hand-fires ``/exec retry``.

This module closes that gap. At lifespan startup — AFTER migrations (needs the
migration-061 counters) and AFTER the node sweep — ``resume_orphaned_executions``
finds every job still ``running``/``executing`` (definitionally an orphan: no
executor exists yet in a freshly-started process), flips it off ``running`` so
``execute_all_nodes``' concurrent-execution guard accepts it, and spawns a
detached drain that resumes at the reset node.

Crash-loop guard. A node that reliably kills the process would otherwise
restart-storm on every boot. ``jobs.resume_attempts`` counts *consecutive*
resume launches that made zero new progress; ``jobs.resume_done_marker`` is the
done-node count at the last launch. When a restart makes no new ``done`` nodes
the counter climbs; once it would exceed ``settings.execution_max_resume_attempts``
the job is marked ``failed`` with ``error_summary='crash_resume_budget_exhausted'``
instead of relaunching. A restart that DOES make progress resets the counter to
1, so the guard only trips on a genuinely poisonous node.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import text

from app.config import settings
from app.database import async_session

logger = logging.getLogger("scaffold.execution_resume")

# Detached drain tasks. asyncio.create_task holds only a weak ref, so keep a
# strong ref here (same pattern as execution_agent._CLEANUP_TASKS) until each
# task finishes.
_RESUME_TASKS: set[asyncio.Task] = set()

CRASH_RESUME_BUDGET_SUMMARY = "crash_resume_budget_exhausted"


async def _drain_execution(job_id: str) -> None:
    """Re-drive ``execute_all_nodes`` for a resumed job to completion.

    ``execute_all_nodes`` is an SSE async generator; there is no client on the
    startup path, so we just drain it. It self-manages the concurrency slot,
    the job status, and per-node claims, and skips already-``done`` nodes — so
    this resumes at the reset node with zero duplicated work.

    ``model_overrides`` is intentionally ``None``: per-job overrides live only
    in the original request body (not persisted), but every node's routing is
    already pinned in ``dag_nodes.assigned_model``, and any global per-role
    overrides were replayed onto ``settings`` earlier in the same lifespan.
    """
    from app.modules.execution_agent import execute_all_nodes

    try:
        async for _ in execute_all_nodes(job_id):
            pass
        logger.info("crash_resume_drain_complete: job=%s", job_id)
    except Exception:
        logger.exception("crash_resume_drain_failed: job=%s", job_id)


def _spawn_resume_drain(job_id: str) -> asyncio.Task:
    """Schedule a detached drain with a strong ref so it isn't GC'd mid-run."""
    task = asyncio.create_task(_drain_execution(job_id))
    _RESUME_TASKS.add(task)
    task.add_done_callback(_RESUME_TASKS.discard)
    return task


async def drain_resume_tasks(timeout: float = 5.0) -> None:
    """Test hook — wait for all in-flight resume drains to complete."""
    if _RESUME_TASKS:
        await asyncio.wait_for(
            asyncio.gather(*list(_RESUME_TASKS), return_exceptions=True),
            timeout=timeout,
        )


async def resume_orphaned_executions(*, spawn: bool = True) -> dict[str, Any]:
    """Find jobs orphaned mid-execution by a crash and resume or fail each.

    Runs once at lifespan startup. Returns a summary dict:

        {
          "skipped": bool,           # True when the valve is off
          "reason": str | None,      # why skipped
          "candidates": int,         # orphaned running/executing jobs found
          "resumed": [job_id, ...],  # relaunched (drain spawned)
          "budget_failed": [job_id, ...],  # crash-loop guard tripped -> failed
        }

    ``spawn=False`` performs the DB claim/fail bookkeeping but does not launch
    the drain tasks (unit-test hook so the counter logic can be asserted
    without running a real DAG).
    """
    if not settings.execution_resume_on_startup_enabled:
        return {
            "skipped": True,
            "reason": "disabled",
            "candidates": 0,
            "resumed": [],
            "budget_failed": [],
        }

    cap = settings.execution_max_resume_attempts
    resumed: list[str] = []
    budget_failed: list[str] = []

    async with async_session() as db:
        # Candidates: a job still running/executing at startup that owns at
        # least one DAG node. In a freshly-started process no executor exists,
        # so any such row is a crash-orphan. Umbrella parents (status
        # 'aggregating', no DAG) and jobs still in early phases are excluded.
        candidates = (await db.execute(text("""
            SELECT j.id AS id,
                   j.resume_attempts AS resume_attempts,
                   j.resume_done_marker AS resume_done_marker
              FROM jobs j
             WHERE j.status IN ('running', 'executing')
               AND EXISTS (SELECT 1 FROM dag_nodes n WHERE n.job_id = j.id)
             ORDER BY j.updated_at ASC
        """))).mappings().all()

        for row in candidates:
            job_id = str(row["id"])

            done_count = (await db.execute(text(
                "SELECT COUNT(*) FROM dag_nodes "
                "WHERE job_id = :jid AND status IN ('done', 'skipped')"
            ), {"jid": job_id})).scalar() or 0

            # Progress-aware attempt counter: a restart that produced new
            # terminal nodes since the last launch resets the streak to 1;
            # otherwise the zero-progress streak climbs.
            made_progress = done_count > (row["resume_done_marker"] or 0)
            attempts = 1 if made_progress else (row["resume_attempts"] or 0) + 1

            if attempts > cap:
                # Crash-loop guard tripped — fail instead of relaunching.
                # Guarded on the current status so a racing writer that already
                # moved the row wins. completed_at is auto-stamped by the
                # migration-047 terminal trigger.
                failed = (await db.execute(text("""
                    UPDATE jobs
                       SET status = 'failed',
                           error_summary = :summary,
                           updated_at = NOW()
                     WHERE id = :jid AND status IN ('running', 'executing')
                     RETURNING id
                """), {"jid": job_id, "summary": CRASH_RESUME_BUDGET_SUMMARY})).first()
                if failed is not None:
                    budget_failed.append(job_id)
                    logger.warning(
                        "crash_resume_budget_exhausted: job=%s attempts=%d cap=%d "
                        "done_nodes=%d -> failed",
                        job_id, attempts, cap, done_count,
                    )
                continue

            # Claim: flip off 'running' (execute_all_nodes' guard rejects a job
            # already 'running') to 'executing', and record the counter +
            # progress marker atomically. Guarded so a racing owner wins.
            claimed = (await db.execute(text("""
                UPDATE jobs
                   SET status = 'executing',
                       resume_attempts = :attempts,
                       resume_done_marker = :marker,
                       updated_at = NOW()
                 WHERE id = :jid AND status IN ('running', 'executing')
                 RETURNING id
            """), {
                "jid": job_id, "attempts": attempts, "marker": done_count,
            })).first()
            if claimed is None:
                continue
            resumed.append(job_id)
            logger.info(
                "crash_resume_relaunch: job=%s attempt=%d/%d done_nodes=%d "
                "made_progress=%s",
                job_id, attempts, cap, done_count, made_progress,
            )

        await db.commit()

    # Spawn drains only after the claims are committed — the detached tasks
    # open their own sessions and must see the 'executing' status.
    if spawn:
        for job_id in resumed:
            _spawn_resume_drain(job_id)

    if resumed or budget_failed:
        logger.info(
            "crash_resume_sweep: candidates=%d resumed=%d budget_failed=%d",
            len(candidates), len(resumed), len(budget_failed),
        )

    return {
        "skipped": False,
        "reason": None,
        "candidates": len(candidates),
        "resumed": resumed,
        "budget_failed": budget_failed,
    }
