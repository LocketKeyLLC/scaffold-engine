"""§17.568 — parallel-frontier executor (_run_parallel_frontier).

Drives the parallel loop over a diamond DAG (T1 → {T2,T3} → T4) with stubbed
claim/exec/all-done/summary so it runs without a real DB or LLM. Asserts:
  - independent siblings (T2,T3) run CONCURRENTLY (a 2-party barrier releases;
    a serial executor would time out → T4 never runs → assertion fails),
  - dependencies are respected (T4 only starts after T2 AND T3 are done),
  - the loop finalizes exactly once (via _all_nodes_done → one summary),
  - node_done streams for every node + a terminal pipeline_complete.

The terminal path checks _all_nodes_done DIRECTLY (a worker's idempotent
autocomplete may already have flipped the job to 'completed' — §17.568 live
probe caught a no-preclaim execute_next_node finalize wrongly returning 'error'
under that race), so the stubs patch _all_nodes_done.
"""
import asyncio
import time

from tests._execution_agent_shared import *  # noqa: F401,F403

from app.config import settings
from app.modules import execution_agent as ea


class _DummyDB:
    async def __aenter__(self):
        return AsyncMock()  # noqa: F405

    async def __aexit__(self, *a):
        return False


@pytest.mark.smoke  # noqa: F405
@pytest.mark.asyncio  # noqa: F405
async def test_parallel_frontier_diamond_concurrency_and_deps(monkeypatch):
    DAG = {"T1": [], "T2": ["T1"], "T3": ["T1"], "T4": ["T2", "T3"]}
    done: set[str] = set()
    claimed: set[str] = set()
    started: list[str] = []
    summary_calls = {"n": 0}
    both_siblings_started = asyncio.Event()

    async def fake_claim(db, job_id, limit):
        ready = [k for k, deps in DAG.items()
                 if k not in claimed and all(d in done for d in deps)]
        take = ready[:limit]
        claimed.update(take)
        return [{"id": k, "node_key": k, "title": k, "tool": "LLM",
                 "depends_on": DAG[k]} for k in take]

    async def fake_exec(job_id, *, model_overrides=None, preclaimed_node=None):
        assert preclaimed_node is not None, "happy path never finalizes via execute_next_node"
        nk = preclaimed_node["node_key"]
        started.append(nk)
        if nk == "T4":
            assert {"T2", "T3"} <= done, "T4 ran before its deps completed"
        if {"T2", "T3"} <= set(started):
            both_siblings_started.set()
        if nk in ("T2", "T3"):
            # Both siblings must be in flight at once → proves concurrency.
            # A serial loop never starts the 2nd → this times out → failed node.
            await asyncio.wait_for(both_siblings_started.wait(), timeout=5)
        done.add(nk)
        return {"status": "done", "node_key": nk, "title": nk, "tool": "LLM"}

    async def fake_all_done(db, job_id):
        return len(done) == 4

    async def fake_summary(job_id, node_results, elapsed_ms, sf, extra_fields=None):
        summary_calls["n"] += 1
        return {"total_nodes": len(node_results),
                "passed": len(node_results), "failed": 0}

    monkeypatch.setattr(ea, "async_session", lambda: _DummyDB())
    monkeypatch.setattr(ea, "_claim_ready_nodes", fake_claim)
    monkeypatch.setattr(ea, "execute_next_node", fake_exec)
    monkeypatch.setattr(ea, "_all_nodes_done", fake_all_done)
    monkeypatch.setattr(ea, "_build_pipeline_summary", fake_summary)
    monkeypatch.setattr(settings, "parallel_execution_max_inflight", 4)
    monkeypatch.setattr(settings, "sse_keepalive_seconds", 0.2)

    events = []
    async for ev in ea._run_parallel_frontier(
        "job-x", model_overrides=None, t0=time.monotonic(), retry_budget=5,
    ):
        events.append(ev)

    assert set(started) == {"T1", "T2", "T3", "T4"}        # all executed
    assert both_siblings_started.is_set()                  # T2,T3 concurrent
    assert summary_calls["n"] == 1                         # finalized once
    joined = "".join(events)
    for k in ("T1", "T2", "T3", "T4"):
        assert f'"node_key": "{k}"' in joined              # node_done streamed
    assert "pipeline_complete" in joined


@pytest.mark.smoke  # noqa: F405
@pytest.mark.asyncio  # noqa: F405
async def test_parallel_frontier_blocked_terminal(monkeypatch):
    """No completable nodes + not all done → loop finalizes 'blocked' via the
    execute_next_node terminal path (job still 'running')."""
    async def fake_claim(db, job_id, limit):
        return []                                          # nothing claimable

    async def fake_all_done(db, job_id):
        return False                                       # → blocked branch

    async def fake_exec(job_id, *, model_overrides=None, preclaimed_node=None):
        assert preclaimed_node is None
        return {"status": "blocked", "message": "deps unsatisfied"}

    monkeypatch.setattr(ea, "async_session", lambda: _DummyDB())
    monkeypatch.setattr(ea, "_claim_ready_nodes", fake_claim)
    monkeypatch.setattr(ea, "_all_nodes_done", fake_all_done)
    monkeypatch.setattr(ea, "execute_next_node", fake_exec)
    monkeypatch.setattr(settings, "sse_keepalive_seconds", 0.2)

    events = []
    async for ev in ea._run_parallel_frontier(
        "job-y", model_overrides=None, t0=time.monotonic(), retry_budget=5,
    ):
        events.append(ev)
    assert any("blocked" in e for e in events)
