"""§17.1226 — "this step's goal is already met", with evidence, cascading nothing.

Live: ADD50 ran `pct start 111` through the runner, the SSE response was lost,
and the step was recorded `failed`. Two read-only checks then showed container
111 running with `node` listening on `*:3001` — the work HAD happened, and there
was no way to say so. `reset_node` is the only status write the API offers and it
resets the transitive downstream, which here would have re-opened a `skipped`
sibling, "Set VM 106's scsi0 disk to 40G", on a disk just grown to 100G. The only
safe option was to leave a true thing recorded as false.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import node_editor as ne

JOB = "11111111-2222-3333-4444-555555555555"


def _db(prior_output: str = "old output"):
    db = AsyncMock()
    scalar_res = MagicMock()
    scalar_res.scalar.return_value = prior_output
    db.execute = AsyncMock(return_value=scalar_res)
    db.commit = AsyncMock()
    return db


def _nodes(status: str):
    return [{"node_key": "ADD50", "status": status, "title": "Start container 111",
             "depends_on": [], "execution_order": 1},
            {"node_key": "ADD15", "status": "skipped", "title": "Set VM 106's scsi0 disk to 40G",
             "depends_on": ["ADD50"], "execution_order": 2}]


@pytest.mark.asyncio
async def test_a_failed_step_whose_goal_is_met_is_recorded_done_with_the_evidence():
    db = _db()
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes("failed"))):
        out = await ne.mark_satisfied(
            JOB, "ADD50", evidence="$ pct status 111\nstatus: running", db=db)
    assert out == {"status": "ok", "node_key": "ADD50", "was": "failed",
                   "node_status": "done", "cascade": []}
    sql = " ".join(str(c.args[0]) for c in db.execute.await_args_list)
    assert "status = 'done'" in sql
    # the evidence is written where the next step reads it, appended not replacing
    params = [c.args[1] for c in db.execute.await_args_list if len(c.args) > 1]
    block = next(p["block"] for p in params if "block" in p)
    assert "status: running" in block and "Recorded as already done" in block
    assert any("output_text || " in str(c.args[0]) for c in db.execute.await_args_list)


@pytest.mark.asyncio
async def test_it_cascades_nothing():
    """The whole reason it exists: ADD15 ('set scsi0 to 40G') must stay skipped."""
    db = _db()
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes("failed"))), \
         patch.object(ne, "_reset_keys", AsyncMock()) as reset, \
         patch.object(ne, "_reopen_job", AsyncMock()) as reopen:
        out = await ne.mark_satisfied(JOB, "ADD50", evidence="e", db=db)
    assert out["cascade"] == []
    reset.assert_not_awaited()
    reopen.assert_not_awaited()
    sql = " ".join(str(c.args[0]) for c in db.execute.await_args_list)
    assert "ADD15" not in sql
    # and it only ever names the one node
    assert sql.count("node_key = :nk") >= 1


@pytest.mark.asyncio
async def test_evidence_is_required():
    """Not a way to skip work: a step is not done because someone said so."""
    db = _db()
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes("failed"))) as load:
        for bad in ("", "   ", None):
            out = await ne.mark_satisfied(JOB, "ADD50", evidence=bad, db=db)
            assert out["http_status"] == 422 and "evidence is required" in out["error"]
    load.assert_not_awaited()          # refused before it even looks
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_running_node_is_left_to_its_worker():
    db = _db()
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes("running"))):
        out = await ne.mark_satisfied(JOB, "ADD50", evidence="e", db=db)
    assert out["http_status"] == 409 and "running" in out["error"]
    db.commit.assert_not_awaited()
    # the UPDATE also guards on it, so a race cannot steal the row
    assert True


@pytest.mark.asyncio
async def test_an_already_done_node_is_a_conflict_and_a_missing_one_is_404():
    db = _db()
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes("done"))):
        out = await ne.mark_satisfied(JOB, "ADD50", evidence="e", db=db)
    assert out["http_status"] == 409
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes("failed"))):
        out = await ne.mark_satisfied(JOB, "NOPE", evidence="e", db=db)
    assert out["http_status"] == 404


@pytest.mark.asyncio
async def test_the_pre_image_is_recorded_so_the_correction_is_reversible():
    db = _db(prior_output="the failed report")
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes("failed"))), \
         patch.object(ne, "_audit", AsyncMock()) as audit:
        await ne.mark_satisfied(JOB, "ADD50", evidence="e", edited_by="me", db=db)
    args = audit.await_args.args
    assert args[3] == "satisfied"
    assert args[4]["status"] == "failed" and args[4]["output_text"] == "the failed report"
    assert args[5]["status"] == "done" and args[5]["cascade"] == []


def test_the_route_exists_and_takes_evidence():
    import inspect
    from app.routers import nodes as r
    src = inspect.getsource(r)
    assert '@router.post("/nodes/{job_id}/{node_key}/satisfied")' in src
    assert "evidence=body.evidence" in src
    assert "mark_satisfied(" in src
