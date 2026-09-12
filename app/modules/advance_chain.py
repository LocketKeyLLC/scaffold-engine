"""§17.1036 — the approval chain belongs to the server, not the browser.

Live (§17.1035 E2E): the SPA's approve ran ``await /ideate/confirm`` and only
then ``await /dag``. The page was closed five seconds after the click, so the
second request was never sent, and the job sat in ``planning`` with no plan —
the page saying "Researching and drawing the plan" indefinitely, nothing in
the SPA able to restart it, and the reaper's only opinion on a stale
``planning`` job being to CANCEL it after ``planning_stale_minutes``. The OWUI
pipeline's ``_handle_confirm`` and ``/jobs/{id}/advance`` (an SSE response
whose generator dies with the client) have the same shape: a multi-step chain
whose continuation lives in a client that may go away.

This module owns the chain as a DETACHED task (the §17.1007 run-broker idea,
one level up): ``start_advance_chain`` records intent in ``jobs.metadata``,
runs research → plan → (assist start | execute) in fresh sessions, and every
phase is a no-op when its outcome already exists, so calling it twice — or
from two surfaces — is safe. ``resume_stranded_planning`` finds jobs that are
``planning`` with no plan and no live chain and starts one, on a short loop,
before the reaper's cancel threshold can reach them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.config import settings
from app.database import async_session

logger = logging.getLogger("scaffold")

# In-process registry of live chains: job_id -> task. A restart empties it,
# which is exactly when the resume loop's marker-age test takes over.
_CHAINS: dict[str, asyncio.Task] = {}
_TASKS: set[asyncio.Task] = set()

PHASES = ("research", "planning", "assist", "execute", "done", "error")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _job_state(job_id: str) -> Optional[dict]:
    async with async_session() as db:
        row = (await db.execute(text("""
            SELECT j.status, j.metadata,
                   (SELECT COUNT(*) FROM dag_nodes WHERE job_id = j.id) AS node_count,
                   EXISTS (SELECT 1 FROM assist_sessions s WHERE s.job_id = j.id) AS has_session
              FROM jobs j WHERE j.id = :jid
        """), {"jid": job_id})).mappings().first()
    if not row:
        return None
    md = row["metadata"]
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except (ValueError, TypeError):
            md = {}
    return {"status": row["status"], "node_count": int(row["node_count"] or 0),
            "has_session": bool(row["has_session"]), "metadata": md or {}}


async def _mark(job_id: str, **fields: Any) -> None:
    """Merge chain state into ``jobs.metadata.advance_chain`` (never clobbers
    other metadata keys)."""
    try:
        async with async_session() as db:
            cur = (await db.execute(text(
                "SELECT metadata->'advance_chain' FROM jobs WHERE id = :jid"),
                {"jid": job_id})).scalar()
            if isinstance(cur, str):
                try:
                    cur = json.loads(cur)
                except (ValueError, TypeError):
                    cur = {}
            state = dict(cur or {})
            state.update(fields)
            state["updated_at"] = _now()
            await db.execute(text("""
                UPDATE jobs
                   SET metadata = COALESCE(metadata, '{}'::jsonb)
                                  || jsonb_build_object('advance_chain', CAST(:st AS jsonb))
                 WHERE id = :jid
            """), {"jid": job_id, "st": json.dumps(state)})
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — the marker is observability, not control flow
        logger.warning("advance_chain_mark_failed job_id=%s err=%r", job_id, exc)


async def chain_state(job_id: str) -> dict:
    """What the chain is doing for this job, for a client to poll."""
    st = await _job_state(job_id)
    if st is None:
        return {"job_id": job_id, "chain": "unknown"}
    marker = (st["metadata"] or {}).get("advance_chain") or {}
    live = job_id in _CHAINS and not _CHAINS[job_id].done()
    return {
        "job_id": job_id,
        "chain": "running" if live else ("done" if marker.get("phase") == "done"
                                         else ("error" if marker.get("phase") == "error"
                                               else ("idle" if not marker else "stale"))),
        "phase": marker.get("phase"),
        "error": marker.get("error"),
        "status": st["status"],
        "node_count": st["node_count"],
        "has_session": st["has_session"],
    }


async def _run_chain(job_id: str, *, feedback: Optional[str], model_overrides: Optional[dict],
                     assist: bool, execute: bool, push_to_github: bool) -> None:
    from app.modules.ideation_workflow import research_and_compile
    from app.modules.dag_generator import generate_dag

    try:
        st = await _job_state(job_id)
        if st is None:
            await _mark(job_id, phase="error", error="job not found")
            return

        # Phase 1 — research + compile, only from the approval gate.
        if st["status"] == "awaiting_confirmation":
            await _mark(job_id, phase="research")
            async with async_session() as db:
                res = await research_and_compile(
                    job_id, db, user_feedback=feedback,
                    push_to_github=push_to_github, model_overrides=model_overrides,
                )
            if isinstance(res, dict) and "error" in res:
                await _mark(job_id, phase="error", error=f"research: {res['error']}")
                logger.warning("advance_chain_error job_id=%s phase=research err=%s",
                               job_id, res["error"])
                return
            st = await _job_state(job_id) or st

        # Phase 2 — plan, only when there is none. generate_dag serialises
        # concurrent callers under FOR UPDATE and refuses a job outside
        # planning/running, so a second caller cannot double-plan.
        if st["status"] == "planning" and st["node_count"] == 0:
            await _mark(job_id, phase="planning")
            async with async_session() as db:
                dag = await generate_dag(job_id, db, model_overrides=model_overrides)
            if isinstance(dag, dict) and "error" in dag:
                await _mark(job_id, phase="error", error=f"planning: {dag['error']}")
                logger.warning("advance_chain_error job_id=%s phase=planning err=%s",
                               job_id, dag["error"])
                return
            st = await _job_state(job_id) or st

        # Phase 3 — the mode the operator chose at the gate.
        if assist and st["node_count"] > 0 and not st["has_session"]:
            await _mark(job_id, phase="assist")
            from app.modules import assist_agent
            async with async_session() as db:
                await assist_agent.start_assist_session(job_id=job_id, db=db)
        elif execute and st["node_count"] > 0 and st["status"] == "executing":
            await _mark(job_id, phase="execute")
            from app.modules.execution_agent import execute_all_nodes
            async for _chunk in execute_all_nodes(job_id, model_overrides=model_overrides):
                pass

        await _mark(job_id, phase="done")
        logger.info("advance_chain_done job_id=%s assist=%s execute=%s", job_id, assist, execute)
    except asyncio.CancelledError:
        await _mark(job_id, phase="error", error="cancelled (orchestrator shutdown)")
        raise
    except Exception as exc:  # noqa: BLE001 — a chain failure is recorded, never raised into the loop
        logger.exception("advance_chain_crashed job_id=%s", job_id)
        await _mark(job_id, phase="error", error=repr(exc)[:300])


async def start_advance_chain(
    job_id: str, *, feedback: Optional[str] = None, model_overrides: Optional[dict] = None,
    assist: bool = False, execute: bool = False, push_to_github: bool = False,
    source: str = "api",
) -> dict:
    """Start (or report) the detached chain for a job. Idempotent: a live
    chain is returned as-is; a finished chain with nothing left to do reports
    ``done`` without starting anything."""
    live = _CHAINS.get(job_id)
    if live is not None and not live.done():
        state = await chain_state(job_id)
        state["started"] = False
        return state
    st = await _job_state(job_id)
    if st is None:
        return {"job_id": job_id, "chain": "unknown", "started": False}
    needs = (st["status"] == "awaiting_confirmation"
             or (st["status"] == "planning" and st["node_count"] == 0)
             or (assist and st["node_count"] > 0 and not st["has_session"]
                 and st["status"] in ("executing", "planning"))
             or (execute and st["node_count"] > 0 and st["status"] == "executing"))
    if not needs:
        state = await chain_state(job_id)
        state["started"] = False
        return state
    await _mark(job_id, phase="starting", started_at=_now(), source=source,
                assist=assist, execute=execute, error=None)
    task = asyncio.create_task(
        _run_chain(job_id, feedback=feedback, model_overrides=model_overrides,
                   assist=assist, execute=execute, push_to_github=push_to_github),
        name=f"advance-chain-{job_id[:8]}",
    )
    _CHAINS[job_id] = task
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    logger.info("advance_chain_started job_id=%s source=%s status=%s nodes=%d assist=%s",
                job_id, source, st["status"], st["node_count"], assist)
    state = await chain_state(job_id)
    state["started"] = True
    return state


_STRANDED_SQL = """
    SELECT j.id, j.metadata
      FROM jobs j
     WHERE j.status = 'planning'
       AND NOT EXISTS (SELECT 1 FROM dag_nodes n WHERE n.job_id = j.id)
       AND j.updated_at < NOW() - make_interval(mins => :older_min)
     ORDER BY j.updated_at ASC
     LIMIT :lim
"""


async def resume_stranded_planning(*, older_than_minutes: Optional[int] = None,
                                   limit: int = 3) -> list[str]:
    """Jobs in ``planning`` with no plan, no live chain and no fresh marker →
    start a planning-only chain. Returns the job ids resumed."""
    older = int(older_than_minutes if older_than_minutes is not None
                else settings.advance_resume_after_minutes)
    async with async_session() as db:
        rows = (await db.execute(text(_STRANDED_SQL),
                                 {"older_min": older, "lim": int(limit)})).mappings().all()
    resumed: list[str] = []
    for r in rows:
        jid = str(r["id"])
        live = _CHAINS.get(jid)
        if live is not None and not live.done():
            continue
        md = r["metadata"]
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except (ValueError, TypeError):
                md = {}
        marker = (md or {}).get("advance_chain") or {}
        # A marker younger than the threshold belongs to a chain that may still
        # be alive in another process (or just crashed); leave it one interval.
        upd = marker.get("updated_at")
        if upd and marker.get("phase") not in ("done", "error", None):
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(upd)).total_seconds()
                if age < older * 60:
                    continue
            except ValueError:
                pass
        assist = bool(marker.get("assist", False))
        logger.warning("advance_chain_resume job_id=%s stranded_in=planning marker_phase=%s",
                       jid, marker.get("phase"))
        await start_advance_chain(jid, assist=assist, source="resume")
        resumed.append(jid)
    return resumed


async def _resume_loop() -> None:
    while True:
        try:
            await resume_stranded_planning()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("advance_resume_loop_error err=%r", exc)
        await asyncio.sleep(settings.advance_resume_interval_seconds)


def start_advance_resume_task() -> asyncio.Task:
    task = asyncio.create_task(_resume_loop(), name="advance-chain-resume")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    logger.info("advance_resume_task_started interval_s=%d after_min=%d",
                settings.advance_resume_interval_seconds, settings.advance_resume_after_minutes)
    return task
