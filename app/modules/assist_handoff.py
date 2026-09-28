"""Assist handoff to the autonomous executor — extracted from assist_agent.py.

§17.856 (audit "assist decomposition") — hand a step (or all remaining steps) off
to the autonomous execution agent and stream the run as SSE (handoff_step), plus
the fire-and-forget background variant (spawn_handoff_background). The executor
calls (execute_all_nodes / execute_next_node) are function-local imports that move
with the body. Self-contained; every name re-exported from assist_agent.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import asyncio
import json
import logging

from sqlalchemy import text

from app.database import async_session
from app.modules.job_state import transition

logger = logging.getLogger("scaffold.assist")


async def handoff_step(
    *, session_id: str, node_key: str, mode: str, db
) -> AsyncGenerator[str, None]:
    """Hand a node back to the autonomous executor.

    `mode` is 'single' (one node, then back to assist) or 'all_remaining'
    (autonomous takes the rest of the DAG).

    Yields SSE-formatted strings from the underlying executor.
    """
    if mode not in ("single", "all_remaining"):
        raise ValueError(f"mode must be 'single' or 'all_remaining', got {mode!r}")
    sess = (await db.execute(
        text("""
            SELECT id, job_id FROM assist_sessions
             WHERE id = :sid AND status = 'active'
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"session not active: {session_id}")
    job_id = str(sess["job_id"])

    # Mark step(s) as handed_off so assist won't re-claim them.
    if mode == "single":
        await db.execute(
            text("""
                UPDATE assist_steps SET status = 'handed_off', updated_at = NOW()
                 WHERE session_id = :sid AND node_key = :nk
                   AND status IN ('pending', 'presented')
            """),
            {"sid": session_id, "nk": node_key},
        )
    else:
        await db.execute(
            text("""
                UPDATE assist_steps SET status = 'handed_off', updated_at = NOW()
                 WHERE session_id = :sid
                   AND status IN ('pending', 'presented')
            """),
            {"sid": session_id},
        )
    await db.commit()

    # Switch the job out of assisted_* into 'executing' so the autonomous
    # executor's status whitelist accepts it. We flip back to assist on
    # completion (single mode) or leave it in autonomous (all_remaining).
    # §17.1107 (ledger S-3) — guarded: a job that reached a terminal status
    # between the operator's click and this write stays there.
    async with async_session() as db2:
        await transition(db2, job_id, to="executing", reason="assist_handoff")
        await db2.commit()

    yield _sse("assist_handoff_started", {
        "session_id": session_id,
        "node_key": node_key,
        "mode": mode,
    })

    # Defer import to avoid a heavy module-level dep on execution_agent.
    from app.modules.execution_agent import execute_all_nodes, execute_next_node

    try:
        if mode == "single":
            # §17.594 — single handoff must run EXACTLY the one node, not the
            # whole remaining DAG. Previously this called the unscoped
            # execute_all_nodes(job_id), which autonomously drained every other
            # 'pending' node — the opposite of "delegate this one step". Claim
            # the target node atomically (pending -> running) and drive it
            # through the per-node autonomous executor via `preclaimed_node`,
            # which skips execute_next_node's own claim. That executor
            # auto-completes the job only if this node was the last remaining
            # one; otherwise the other pending nodes are left untouched and
            # control returns to assist via the restore below. The presented
            # step handed off here is dep-satisfied by assist's DAG walk.
            async with async_session() as dbc:
                claimed = (await dbc.execute(
                    text("""
                        UPDATE dag_nodes
                           SET status = 'running', started_at = NOW()
                         WHERE job_id = :jid AND node_key = :nk
                           AND status = 'pending'
                        RETURNING id, node_key, title, node_type, depends_on,
                                  assigned_model, prompt_template, execution_order,
                                  tool, domain, retry_count, last_verification_reason
                    """),
                    {"jid": job_id, "nk": node_key},
                )).mappings().first()
                await dbc.commit()

            if claimed is None:
                # Node already ran or isn't pending — nothing to hand off.
                yield _sse("assist_handoff_noop", {
                    "session_id": session_id,
                    "node_key": node_key,
                    "reason": "node not pending",
                })
            else:
                yield _sse("node_start", {
                    "node_key": node_key,
                    "title": claimed.get("title"),
                })
                result = await execute_next_node(
                    job_id, preclaimed_node=dict(claimed),
                )
                if result.get("status") in ("done", "skipped"):
                    yield _sse("node_done", {
                        "node_key": node_key,
                        "title": result.get("title"),
                        "verified": result.get("verified", True),
                        "job_complete": result.get("job_complete", False),
                    })
                else:
                    yield _sse("node_failed", {
                        "node_key": node_key,
                        "title": result.get("title"),
                        "error": result.get("error") or result.get("message"),
                        "reason": result.get("reason"),
                    })
        else:
            async for ev in execute_all_nodes(job_id):
                yield ev
    finally:
        # On return, restore assist mode unless all_remaining took over.
        if mode == "single":
            # §17.410 — shield the restore so a client disconnect mid-handoff
            # can't abort it. The bare awaits here used to run unprotected: a
            # CancelledError (SSE disconnect while the handed-off node executes)
            # interrupted the restore, leaving the job stuck in 'executing'
            # instead of 'assisted_executing' until the reaper. Mirrors the
            # cancel-safe finalize in research_state._run_with_session_lifecycle.
            async def _restore_assist_mode() -> None:
                async with async_session() as db3:
                    # Only restore if the session is still active.
                    still = (await db3.execute(
                        text("SELECT status FROM assist_sessions WHERE id = :sid"),
                        {"sid": session_id},
                    )).scalar()
                    if still == "active":
                        await db3.execute(
                            text("UPDATE jobs SET status = 'assisted_executing', updated_at = NOW() "
                                 "WHERE id = :jid AND status NOT IN ('completed', 'failed', 'cancelled')"),
                            {"jid": job_id},
                        )
                        await db3.commit()

            try:
                await asyncio.shield(_restore_assist_mode())
            except asyncio.CancelledError:
                # Caller-side cancellation hit while the shielded restore runs;
                # the UPDATE continues on the loop. Re-raise to preserve
                # asyncio cancellation semantics.
                logger.warning(
                    "assist_handoff_restore_cancel_propagated_but_shielded: "
                    "session_id=%s job_id=%s — UPDATE continues on loop",
                    session_id, job_id,
                )
                raise

        # §17.599 — if the handoff drove the job to terminal 'completed',
        # finalize the assist session so /assist/_chatmap stops auto-routing
        # plain chat into a done session and the idle reaper doesn't mislabel
        # it 'abandoned'. Covers both modes: single-mode auto-completes only on
        # the last node (§17.594), all_remaining completes when the DAG
        # finishes. Deliberately does NOT re-compile (the executor already set
        # compiled_output) — this only settles the session row. Shielded so a
        # client disconnect can't strand the session 'active'.
        async def _finalize_session_if_job_done() -> None:
            async with async_session() as db4:
                jstatus = (await db4.execute(
                    text("SELECT status FROM jobs WHERE id = :jid"),
                    {"jid": job_id},
                )).scalar()
                if jstatus == "completed":
                    await db4.execute(
                        text(
                            "UPDATE assist_sessions SET status = 'completed', "
                            "completed_at = NOW(), updated_at = NOW() "
                            "WHERE id = :sid AND status IN ('active', 'paused')"
                        ),
                        {"sid": session_id},
                    )
                    await db4.commit()

        try:
            await asyncio.shield(_finalize_session_if_job_done())
        except asyncio.CancelledError:
            logger.warning(
                "assist_handoff_finalize_cancel_propagated_but_shielded: "
                "session_id=%s job_id=%s", session_id, job_id,
            )
            raise

    yield _sse("assist_handoff_done", {
        "session_id": session_id,
        "node_key": node_key,
        "mode": mode,
    })


# §17.621 (audit #20) — strong refs to fire-and-forget auto-handoff tasks so
# they survive GC (mirrors web.routes / research_agent background-task sets).
_HANDOFF_BACKGROUND_TASKS: set = set()


def spawn_handoff_background(*, session_id: str, node_key: str, mode: str) -> "asyncio.Task":
    """§17.621 (audit #20) — drive ``handoff_step`` to completion on the event
    loop in a background task, consuming (and discarding) its SSE frames.

    This is what makes the ``handoff_policy`` auto values do something: on an
    operator skip with ``auto_on_skip`` / ``auto_all_remaining``, the router
    hands the step to the autonomous executor without blocking the JSON /submit
    response. Uses its OWN short-lived session — ``handoff_step`` only touches
    the passed ``db`` briefly at the start (it commits, releasing the connection)
    and runs the long execution on independent sessions, so nothing is pinned.
    Fire-and-forget + fail-soft: an executor error is logged, never raised.
    """
    async def _run() -> None:
        try:
            async with async_session() as hdb:
                async for _ in handoff_step(
                    session_id=session_id, node_key=node_key, mode=mode, db=hdb,
                ):
                    pass
        except Exception:
            logger.exception(
                "auto_handoff_background_failed: session=%s node=%s mode=%s",
                session_id, node_key, mode,
            )

    task = asyncio.create_task(_run())
    _HANDOFF_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_HANDOFF_BACKGROUND_TASKS.discard)
    return task


def _sse(event_type: str, payload: dict) -> str:
    """SSE wire format. Same shape as research_agent / execution_agent."""
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


# ── §17.1190 — hand the REST of the plan to the engine ────────────────────
# `handoff_step(mode="all_remaining")` has existed since §17.856 and routes
# into `execute_all_nodes` — the path that now stops to ask at a decision
# (§17.1184) and at every step that changes a machine (§17.1186). The operator
# could not reach it: the SPA only ever posted `mode:"single"`, and the
# `auto_all_remaining` handoff POLICY fires on a skip and is fixed at session
# start. These two give the walkthrough an honest control — one that says what
# would happen BEFORE it happens, and refuses when the answer is "nothing".


async def preview_all_remaining(db, session_id: str) -> dict:
    """What handing the rest of this plan to the engine would mean, right now.

    Deterministic, read-only, no model: the steps left, how many of them change
    a machine, whether a runner's write channel is open to carry them, and
    whether the §17.624 gate would simply park the job back here (it would, if
    the plan is predominantly hands-on and no channel is open — pressing the
    button then achieves nothing, so the button must say so instead)."""
    from app.modules import supervised_runs
    from app.modules.execution_agent import _classify_dag_executability
    from app.modules.step_classify import step_is_hands_on

    sess = (await db.execute(
        text("SELECT id, job_id, status, current_node_key FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    job_id = str(sess["job_id"])
    rows = (await db.execute(
        text("""SELECT node_key, title, description, prompt_template, tool, node_type
                  FROM dag_nodes WHERE job_id = :jid AND status = 'pending'
                 ORDER BY execution_order"""),
        {"jid": job_id},
    )).mappings().all()
    remaining = [dict(r) for r in rows]
    hands_on = [r for r in remaining if step_is_hands_on(r, shell_backend=False)[0]]
    decisions = [r for r in remaining if str(r.get("node_type") or "") == "decision"]

    ch = await supervised_runs.channel(db)
    runner = getattr(ch[0], "name", None) if ch else None
    allow = list((ch[1].get("allow") or [])) if ch else []
    cls = await _classify_dag_executability(db, job_id)
    would_park = bool(cls.get("hands_on"))

    blocker = ""
    if sess["status"] != "active":
        blocker = f"this walkthrough is {sess['status']}, not active"
    elif not remaining:
        blocker = "every step of this plan is already done or skipped"
    elif would_park:
        blocker = ("the engine would hand this plan straight back: "
                   f"{cls['nonexec']} of {cls['total']} steps change a machine and no runner has an open "
                   "write channel. Open one first — Capabilities → “Let the engine run approved commands”.")
    return {
        "session_id": str(sess["id"]), "job_id": job_id, "session_status": sess["status"],
        "current_node_key": sess["current_node_key"],
        "remaining": len(remaining), "hands_on": len(hands_on), "decisions": len(decisions),
        "remaining_keys": [r["node_key"] for r in remaining][:200],
        "channel_open": ch is not None, "runner": runner, "allow": allow,
        "would_park": would_park, "gate": cls,
        "can_start": not blocker, "blocker": blocker,
    }


async def start_all_remaining(db, session_id: str, node_key: str | None = None) -> dict:
    """Hand every remaining step to the engine and start the run DETACHED.

    The SSE ``/handoff`` path calls ``execute_all_nodes`` inside the response
    generator, so a closed tab would cancel a run of this size — the §17.1007
    hostage bug. This uses the same seam ``/jobs/{id}/decide`` uses:
    ``run_broker.start`` (idempotent per job), and returns at once. The Run
    stage attaches through ``/execute/all``.
    """
    from app.modules import run_broker
    from app.modules.execution_agent import execute_all_nodes

    pre = await preview_all_remaining(db, session_id)
    if not pre["can_start"]:
        return {**pre, "started": False}
    job_id = pre["job_id"]
    await db.execute(
        text("""UPDATE assist_steps SET status = 'handed_off', updated_at = NOW()
                 WHERE session_id = :sid AND status IN ('pending', 'presented')"""),
        {"sid": session_id},
    )
    await db.commit()
    async with async_session() as db2:
        await transition(db2, job_id, to="executing", reason="assist_handoff_all_remaining")
        await db2.commit()
    started = False
    try:
        run_broker.start(job_id, lambda: execute_all_nodes(job_id))
        started = True
    except Exception as exc:                     # the steps are handed off either way
        logger.error("handoff_all_start_failed job=%s err=%r", job_id, exc)
    logger.warning("assist_handoff_all_remaining job=%s session=%s remaining=%d hands_on=%d runner=%s started=%s",
                   job_id, session_id, pre["remaining"], pre["hands_on"], pre["runner"], started)
    return {**pre, "started": started, "node_key": node_key or pre["current_node_key"]}
