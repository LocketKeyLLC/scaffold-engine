"""§17.1008 — a detached run survives a disconnect, against the REAL executor.

The gap this closes
-------------------
§17.1007 detached ``/execute/all`` from its SSE response, and two test files
already cover the pieces: ``test_run_broker.py`` covers the broker's
bookkeeping with a stand-in generator, and ``test_run_broker_disconnect.py``
covers the HTTP disconnect boundary with a real socket but still a stand-in
generator. Both were flagged in §17.1007b as leaving one thing unproven —
whether ``execute_all_nodes`` *itself*, with its slot semaphore, its
short-lived DB sessions, its progress tracker and its ``finally`` that settles
job status, keeps running when the subscriber goes away.

That is the claim an operator actually relies on, and a stand-in generator
cannot make it. This test drives the real executor over the real HTTP surface.

Cost, and why the DAG is seeded rather than planned
---------------------------------------------------
This runs real inference: each node is an LLM call on whatever model
``model_general`` resolves to. Going through ``/ideas`` → ``/dag`` first would
add a full Phase 1 (1–9 minutes) and a planning pass to test neither, so the
job and a deliberately trivial three-node DAG are seeded directly — the same
approach ``test_claim_ready_nodes_parallel.py`` takes.

Marked ``integration`` (deselected by the cloud lane's ``-m "not integration"``)
and given the 900s budget every live test here carries.

Liveness is read over HTTP, never by importing the broker
---------------------------------------------------------
The first version of this file asserted ``run_broker.is_running(job_id)``
directly and failed in three seconds against a perfectly healthy engine. The
run lives in the UVICORN process; pytest runs in a separate ``docker exec``
process, where ``run_broker._runs`` is its own empty dict. It was measuring the
wrong process and would have reported "the run stopped" for every run that ever
worked. ``/exec/status``'s ``detached_running`` is the cross-process answer, and
node state comes from Postgres, which both processes genuinely share.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.database import async_session

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.timeout(900)]

BASE_URL = "http://localhost:8000"
AUTH = {"X-API-Key": os.environ.get("SCAFFOLD_API_KEY", "test-key-for-ci")}


async def _seed_job_with_dag(job_id: str) -> None:
    """A three-node chain with prompts small enough to finish quickly."""
    async with async_session() as db:
        await db.execute(
            text("""
                INSERT INTO jobs (id, title, status, input_text, job_type)
                VALUES (:j, '§17.1008 detached-run probe', 'executing',
                        'Detached-run integration probe', 'legacy')
            """),
            {"j": job_id},
        )
        await db.execute(
            text("""
                INSERT INTO dag_nodes
                    (job_id, node_key, title, node_type, status, depends_on,
                     execution_order, tool, prompt_template)
                VALUES
                    (:j,'T1','name one colour','task','pending','{}',0,'LLM',
                     'Reply with exactly one word: a colour.'),
                    (:j,'T2','name one animal','task','pending','{"T1"}',1,'LLM',
                     'Reply with exactly one word: an animal.'),
                    (:j,'T3','name one number','task','pending','{"T2"}',2,'LLM',
                     'Reply with exactly one word: a number.')
            """),
            {"j": job_id},
        )
        await db.commit()


async def _detached_running(client: httpx.AsyncClient, job_id: str) -> bool:
    """Is a run in flight IN THE SERVER PROCESS? The only honest way to ask
    from here (see the module docstring)."""
    resp = await client.get(f"/exec/status/{job_id}")
    resp.raise_for_status()
    return bool(resp.json().get("detached_running"))


async def _await_run_end(client: httpx.AsyncClient, job_id: str, budget_s: float = 600) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + budget_s
    while loop.time() < deadline:
        if not await _detached_running(client, job_id):
            return True
        await asyncio.sleep(2)
    return False


async def _cleanup(job_id: str) -> None:
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, headers=AUTH, timeout=60) as client:
            await client.post(f"/jobs/{job_id}/cancel", json={})
    except Exception:  # cleanup must not mask a real failure
        pass
    async with async_session() as db:
        await db.execute(text("DELETE FROM dag_nodes WHERE job_id = :j"), {"j": job_id})
        await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
        await db.commit()


async def _node_statuses(job_id: str) -> dict[str, str]:
    async with async_session() as db:
        rows = (await db.execute(
            text("SELECT node_key, status FROM dag_nodes WHERE job_id = :j"),
            {"j": job_id},
        )).all()
    return {r[0]: r[1] for r in rows}


async def test_real_executor_run_survives_a_client_disconnect():
    """Start a real run, walk away mid-node, and confirm the engine carried on.

    Before §17.1007 this is exactly what killed a run: the disconnect cancelled
    the generator, and ``execute_all_nodes``' finally moved the job to
    cancelled. The assertion at the end is that same finally NOT firing.
    """
    job_id = str(uuid.uuid4())
    await _seed_job_with_dag(job_id)
    try:
        # Open the stream, read until the executor has really started work,
        # then abandon it the way a closed tab does.
        saw_start = False
        async with httpx.AsyncClient(base_url=BASE_URL, headers=AUTH, timeout=300) as client:
            async with client.stream("POST", "/execute/all", json={"job_id": job_id}) as resp:
                assert resp.status_code == 200, await resp.aread()
                async for line in resp.aiter_lines():
                    if "node_start" in line or "node_done" in line:
                        saw_start = True
                        break
        assert saw_start, "the executor never reported starting a node"

        async with httpx.AsyncClient(base_url=BASE_URL, headers=AUTH, timeout=60) as probe:
            # The subscriber is gone. The run must not be.
            await asyncio.sleep(2)
            assert await _detached_running(probe, job_id) is True, (
                "the real executor stopped when the client disconnected — the "
                "response is still coupled to the run"
            )
            # Let it finish on its own, with nobody watching.
            assert await _await_run_end(probe, job_id), "run did not finish within 10 minutes"

        statuses = await _node_statuses(job_id)
        assert all(s in ("done", "failed", "skipped") for s in statuses.values()), (
            f"nodes left unfinished after the run ended: {statuses}"
        )
        # The point: work continued PAST the disconnect. T1 alone could have
        # completed before it; T3 could not have.
        assert statuses.get("T3") in ("done", "failed", "skipped"), (
            f"the run stopped at the disconnect instead of continuing: {statuses}"
        )

        async with async_session() as db:
            job_status = (await db.execute(
                text("SELECT status FROM jobs WHERE id = :j"), {"j": job_id},
            )).scalar()
        assert job_status != "cancelled", (
            "the job was cancelled by the disconnect — the pre-§17.1007 behaviour"
        )
    finally:
        await _cleanup(job_id)


async def test_reattaching_to_a_real_run_does_not_start_a_second_one():
    """`run_broker.start` is idempotent per job. Against the real executor that
    matters more than anywhere else: a second walk would re-run every node."""
    job_id = str(uuid.uuid4())
    await _seed_job_with_dag(job_id)
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, headers=AUTH, timeout=300) as client:
            async with client.stream("POST", "/execute/all", json={"job_id": job_id}) as resp:
                async for line in resp.aiter_lines():
                    if "node_start" in line:
                        break

            # Reattach while the first run is still going.
            assert await _detached_running(client, job_id) is True
            async with client.stream("POST", "/execute/all", json={"job_id": job_id}) as resp2:
                assert resp2.status_code == 200
                async for _ in resp2.aiter_lines():
                    break

            status = (await client.get(f"/exec/status/{job_id}")).json()
            assert status["detached_running"] is True

        async with httpx.AsyncClient(base_url=BASE_URL, headers=AUTH, timeout=60) as probe:
            await _await_run_end(probe, job_id)

        # Every node runs exactly once: a second walk would have reset and
        # re-executed them, which shows up as a retry_count above zero.
        async with async_session() as db:
            rows = (await db.execute(
                text("SELECT node_key, retry_count FROM dag_nodes WHERE job_id = :j"),
                {"j": job_id},
            )).all()
        assert all(r[1] == 0 for r in rows), f"nodes were executed more than once: {rows}"
    finally:
        await _cleanup(job_id)
