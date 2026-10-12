"""§17.1496 — a refusal leaves the node where it found it; a run that dies after the claim is recorded.

`resolve_run` claims the node (`pending -> running`) and COMMITS before anything reaches
the machine, so a second "run" cannot double-write. Two things were on the wrong side of
that commit:

- the file-placeholder check (`fill_files`) ran after it, so its `inputs_missing` return
  left the node at `running` under a job still `awaiting_decision`; the caller's rollback
  undid nothing, the next "run" answered `node_gone`, and the orphan reaper's reset started
  the same loop again;
- nothing caught an error after it (the secret store, the runner, the client dropping the
  `POST /decide` request, a restart), so the node stayed `running` with the frame still
  offering "Run" for a block that may already have run.

And `park_awaiting_decision` committed its question even when the status transition was
refused (an operator cancel during the minutes of drafting), leaving a cancelled job with a
live question it could never answer.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import decision_pause as dp
from app.modules import supervised_runs as sr


def _spec():
    return type("S", (), {"name": "pve-runner", "headers": {}})()


def _db(*, first=None, rowcount=1):
    db = AsyncMock()
    res = MagicMock()
    res.mappings.return_value.first.return_value = first
    res.rowcount = rowcount
    db.execute = AsyncMock(return_value=res)
    return db


@pytest.mark.asyncio
async def test_a_file_placeholder_nothing_fills_refuses_before_the_node_is_claimed():
    waiting = {"kind": "run", "runbook": "## Run this\n```\npct exec 111 -- systemctl restart app\n```\n",
               "commands": ["pct exec 111 -- systemctl restart app"], "verify": [], "refused": [], "inputs": [],
               "files": [{"path": "/etc/app/env", "content": "PASSWORD=<MASS_PASSWORD>\n"}]}
    db = AsyncMock()
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), {"allow": ["pct exec"], "sudo": False}))), \
         patch("app.modules.rewrite_guard.destructive_rewrites", new=AsyncMock(return_value=[])), \
         patch("app.modules.address_watch.around_run", new=AsyncMock(return_value=([], {}))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock()) as rb, \
         patch("app.modules.assist_supervised.write_files_on", new=AsyncMock()) as wf:
        out = await sr.resolve_run(db, "j", "T1", "run", waiting, inputs={})
    assert out["outcome"] == "inputs_missing"
    assert out["problems"] and out["problems"][0]["name"] == "MASS_PASSWORD"
    rb.assert_not_awaited()
    wf.assert_not_awaited()
    db.execute.assert_not_awaited(), "the node was not claimed: the refusal left it `pending`"
    db.commit.assert_not_awaited()


def test_the_claim_comes_after_every_refusal_in_the_source():
    """The order is the invariant: no `return {"outcome": …}` between the claim's commit and the run."""
    import inspect
    src = inspect.getsource(sr.resolve_run)
    claim = src.index("SET status = 'running'")
    assert src.index("fill_files(_files, values)") < claim, "the file check runs BEFORE the claim"
    after = src[claim:src.index("supervised_run_started")]
    assert '"outcome": "node_gone"' in after
    assert after.count('"outcome":') == 1, "the only exit between the claim and the run is the claim failing"


@pytest.mark.asyncio
async def test_a_run_that_dies_after_the_claim_is_settled_before_the_error_goes_up():
    waiting = {"kind": "run", "node_key": "T1", "commands": ["pct exec 111 -- true"], "verify": []}
    db = _db(first={"status": "awaiting_decision", "metadata": {"awaiting_decision": waiting}})
    boom = RuntimeError("runner: connection reset")
    with patch.object(sr, "resolve_run", new=AsyncMock(side_effect=boom)), \
         patch.object(sr, "settle_interrupted", new=AsyncMock(return_value=True)) as settle, \
         patch("app.modules.execution_agent.unmet_dependencies", new=AsyncMock(return_value=[])):
        with pytest.raises(RuntimeError):
            await dp.resolve_decision(db, "j", "T1", choice="run")
    settle.assert_awaited_once()
    assert settle.await_args.args[:2] == ("j", "T1") and settle.await_args.args[2] is boom


@pytest.mark.asyncio
async def test_a_cancelled_decide_request_is_settled_too():
    """Starlette cancels the handler when the client drops: CancelledError is a BaseException."""
    waiting = {"kind": "run", "node_key": "T1", "commands": ["pct exec 111 -- true"], "verify": []}
    db = _db(first={"status": "awaiting_decision", "metadata": {"awaiting_decision": waiting}})
    with patch.object(sr, "resolve_run", new=AsyncMock(side_effect=asyncio.CancelledError())), \
         patch.object(sr, "settle_interrupted", new=AsyncMock(return_value=True)) as settle, \
         patch("app.modules.execution_agent.unmet_dependencies", new=AsyncMock(return_value=[])):
        with pytest.raises(asyncio.CancelledError):
            await dp.resolve_decision(db, "j", "T1", choice="run")
    settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_settle_marks_the_running_node_failed_with_the_reason_in_its_own_session():
    db = _db(rowcount=1)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch("app.database.async_session", return_value=cm):
        moved = await sr.settle_interrupted("j", "T1", RuntimeError("runner: connection reset"))
    assert moved is True
    sql, params = db.execute.await_args.args
    assert "SET status = 'failed'" in sql.text and "status = 'running'" in sql.text, "only a node the claim moved"
    assert params["jid"] == "j" and params["nk"] == "T1"
    assert "RuntimeError: runner: connection reset" in params["why"] and "reset the node" in params["why"]
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_settle_never_raises_when_the_database_itself_is_down():
    with patch("app.database.async_session", side_effect=OSError("connection refused")):
        assert await sr.settle_interrupted("j", "T1", RuntimeError("x")) is False


@pytest.mark.asyncio
async def test_a_park_the_job_no_longer_admits_writes_nothing():
    db = _db()
    node = {"node_key": "T2", "title": "Decide the GPU strategy", "depends_on": []}
    with patch("app.modules.decision_pause.transition", AsyncMock(return_value=False)):
        out = await dp.park_awaiting_decision(db, "j", node, dp.fallback_frame(node))
    assert out is None
    db.rollback.assert_awaited()
    db.commit.assert_not_awaited(), "a cancelled job does not get a question it cannot answer"


@pytest.mark.asyncio
async def test_a_park_the_job_admits_still_commits():
    db = _db()
    node = {"node_key": "T2", "title": "Decide the GPU strategy", "depends_on": []}
    with patch("app.modules.decision_pause.transition", AsyncMock(return_value=True)):
        out = await dp.park_awaiting_decision(db, "j", node, dp.fallback_frame(node))
    assert out and out["status"] == "awaiting_decision"
    db.commit.assert_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_policy_reply_that_is_not_an_object_is_no_channel_not_a_500():
    from app.modules import assist_supervised as sw
    spec = _spec()
    tools = [{"name": sw.WRITE_TOOL}, {"name": sw.POLICY_TOOL}, {"name": sw.WRITE_FILE_TOOL}]
    reply = type("R", (), {"text": "[]", "structured": None})()
    with patch("app.modules.mcp_client.list_tools", new=AsyncMock(return_value=tools)), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=reply)):
        sw._policy_cache.pop("pve-runner", None)
        assert await sw.write_policy(spec, use_cache=False) is None
    known, pol = sw.cached_policy(spec)
    assert known and pol is None, "cached as unavailable, like any other broken reply"
    sw._policy_cache.pop("pve-runner", None)
    sw._setup_cache.pop("pve-runner", None)
