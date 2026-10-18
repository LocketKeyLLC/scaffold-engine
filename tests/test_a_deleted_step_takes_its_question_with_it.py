"""§17.1639 — a node edit that removes or settles the step the run is parked on drops that step's frame.

Live (job 0fb4ea7f, 2026-10-18): LT2, an emergency stop held ready during a load test, was deleted while the job was
`awaiting_decision` on it. Only `decide` and `reask` cleared `jobs.metadata.awaiting_decision`, so the job kept a frame
for a step that no longer existed, and still carried it after the next run reached `completed`.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import execution_agent as ea
from app.modules import node_editor as ne


def _db(status: str = "awaiting_decision", parked: str | None = "LT2"):
    db = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = {"status": status, "parked": parked}
    result.mappings.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


def _sql(db) -> list[str]:
    return [str(c.args[0]) for c in db.execute.await_args_list]


@pytest.mark.asyncio
async def test_the_parked_step_loses_its_frame_and_the_job_waits_for_run():
    db = _db()
    with patch("app.modules.job_state.transition", AsyncMock(return_value="blocked")) as tr:
        assert await ne._drop_parked_frame(db, "j", ["LT2"], "deleted") is True
    assert any("- 'awaiting_decision'" in s for s in _sql(db)), _sql(db)
    tr.assert_awaited_once()
    assert tr.await_args.kwargs["to"] == "blocked" and tr.await_args.kwargs["expected_from"] == ("awaiting_decision",)


@pytest.mark.asyncio
async def test_another_steps_frame_is_left_alone():
    db = _db(parked="MC1")
    with patch("app.modules.job_state.transition", AsyncMock()) as tr:
        assert await ne._drop_parked_frame(db, "j", ["LT2"], "deleted") is False
    assert not any("- 'awaiting_decision'" in s for s in _sql(db))
    tr.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stale_frame_on_a_job_not_waiting_is_dropped_without_moving_the_job():
    db = _db(status="completed")
    with patch("app.modules.job_state.transition", AsyncMock()) as tr:
        assert await ne._drop_parked_frame(db, "j", ["LT2"], "deleted") is True
    assert any("- 'awaiting_decision'" in s for s in _sql(db))
    tr.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_reset_and_satisfied_each_drop_it():
    nodes = [{"node_key": "MC1", "status": "done", "depends_on": [], "execution_order": 1, "edit_version": 0,
              "is_deliverable": False},
             {"node_key": "LT2", "status": "pending", "depends_on": [], "execution_order": 2, "edit_version": 0,
              "is_deliverable": False}]
    for op, kwargs in ((ne.delete_node, {}), (ne.reset_node, {}), (ne.mark_satisfied, {"evidence": "qm status 110"})):
        with patch.object(ne, "_load_nodes", AsyncMock(return_value=nodes)), \
             patch.object(ne, "_drop_parked_frame", AsyncMock(return_value=True)) as drop, \
             patch.object(ne, "_audit", AsyncMock()), patch.object(ne, "_renumber", AsyncMock()), \
             patch.object(ne, "_reset_keys", AsyncMock()), patch.object(ne, "_drop_engine_half_marks", AsyncMock()):
            await op("j", "LT2", db=_db(), **kwargs)
        drop.assert_awaited_once()
        assert "LT2" in drop.await_args.args[2], (op.__name__, drop.await_args)


def test_a_completed_job_asks_nothing():
    src = inspect.getsource(ea._flip_job_completed)
    assert "status = 'completed', metadata = COALESCE(metadata, '{}'::jsonb) - 'awaiting_decision'" in src
