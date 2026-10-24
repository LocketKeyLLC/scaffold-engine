"""§17.1788 — a step that imports another step's module stands on that step.

Live (job f00ea97f T20 "walk-forward backtester", ~20 attempts): it used T21's `paper_spy_benchmark_tracker` and T22's
metrics but stood on neither, so neither the step nor its verifier was shown them ("not among the provided upstream
blocks") and T22 was not written. The dependency is the step's own code's: it is added, never closing a cycle.
"""
from __future__ import annotations

import uuid

import pytest

from app.modules import engine_source as es
from app.modules import node_editor

text = node_editor.text

T20_OUT = """```python
# app/modules/paper_walk_forward_backtester.py
from app.modules.paper_spy_benchmark_tracker import get_spy_benchmark
from app.modules.paper_performance_metrics_calculator import get_performance_metrics
from app.modules.paper_point_time_store import get_data_as_of
from app.database import async_session
```"""


@pytest.mark.integration
async def test_imported_sibling_steps_become_dependencies_without_a_cycle():
    from app.database import async_session
    jid = str(uuid.uuid4())
    steps = [("T13", "paper_point_time_store", [], "done"),
             ("T20", "paper_walk_forward_backtester", ["T13"], "failed"),
             ("T21", "paper_spy_benchmark_tracker", [], "done"),
             ("T22", "paper_performance_metrics_calculator", ["T20"], "pending")]   # T22 already stands on T20
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'lab', 'running')"), {"id": jid})
            for i, (k, mod, deps, st) in enumerate(steps):
                await db.execute(text(
                    "INSERT INTO dag_nodes (job_id, node_key, title, status, depends_on, prompt_template, "
                    "execution_order, tool) VALUES (:j, :k, :k, :s, :d, :p, :o, 'CodeGen')"),
                    {"j": jid, "k": k, "s": st, "d": deps, "p": f"Python module `{mod}.py`.", "o": i})
            await db.commit()
        async with async_session() as db:
            added = await es.adopt_imported_siblings(db, jid, "T20", T20_OUT)
        assert added == ["T21"]                    # T13 already upstream; T22 would close a cycle (T22 → T20)
        async with async_session() as db:
            deps = (await db.execute(text("SELECT depends_on FROM dag_nodes WHERE job_id = :j AND node_key = 'T20'"),
                                     {"j": jid})).scalar()
        assert list(deps) == ["T13", "T21"]
        async with async_session() as db:          # idempotent
            assert await es.adopt_imported_siblings(db, jid, "T20", T20_OUT) == []
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


@pytest.mark.asyncio
async def test_the_executor_adopts_after_the_gate():
    from unittest.mock import AsyncMock, patch
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    adopt = AsyncMock(return_value=["T21"])
    with patch("app.modules.engine_source.adopt_imported_siblings", new=adopt):
        await _run(GROUNDED, LIVE_BRIEF)
    adopt.assert_awaited()
    assert adopt.await_args.args[1:3] == ("job-1", "T7")
