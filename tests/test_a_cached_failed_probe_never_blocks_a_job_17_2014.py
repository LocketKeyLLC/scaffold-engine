r"""§17.2014 — a failed probe of the runner, cached, is never the reading that blocks a job.

Live (job 0fb4ea7f, 2026-11-24, RD62): right after the laptop engine was rebuilt, the first `/execute/all` probed
pve-runner while it was still coming back -- `supervised_policy_unavailable runner=pve-runner err=McpError("mcp server
'pve-runner': list_tools failed: ConnectError: All connection attempts failed")`. `write_policy` cached that failure
as None for 60s, and every run inside the window was blocked from the cache:

    hands_on_gate_held job=0fb4ea7f… nonexec=207/209 runner_unreachable=pve-runner: it answered with no write policy

The runner was answering by then; and it had never "answered with no write policy" -- it had not answered at all. So
`channel_state` asks again, fresh, when the cached probe failed (§17.2019: once, no wait), says
WHY the probe failed, and the gate reads the runner before it classifies the steps.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw
from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr
from app.modules.mcp_client import McpError

_TOOLS = [{"name": "run_readonly"}, {"name": "run_supervised"}, {"name": "write_policy"}]


class _Spec:
    name = "pve-runner"
    endpoint = "http://192.0.2.10:8790/mcp/"


def _policy_reply():
    r = MagicMock()
    r.structured = None
    r.text = '{"helper": "23", "allow": ["ANY"], "sudo": true}'
    return r


@pytest.fixture(autouse=True)
def _runner(monkeypatch):
    import app.modules.assist_local_runner as lr
    monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=_Spec()))
    monkeypatch.setattr(sr, "enabled", lambda: True)
    monkeypatch.setattr(sr.settings, "mcp_tool_enabled", True)
    # §17.2019 — the fresh probe is first a TCP connect; the endpoint is TEST-NET, so say "no port to try".
    monkeypatch.setattr(sw, "port_answers", AsyncMock(return_value=None))
    sw.clear_policy_cache()
    yield
    sw.clear_policy_cache()


async def _fail_once_then_cache():
    """The rebuild: one probe fails and is cached."""
    with patch("app.modules.mcp_client.list_tools",
               new=AsyncMock(side_effect=McpError("mcp server 'pve-runner': list_tools failed: ConnectError: "
                                                  "All connection attempts failed"))):
        assert await sw.write_policy(_Spec()) is None
    assert sw.cached_policy(_Spec()) == (True, None)


@pytest.mark.asyncio
async def test_a_runner_that_answers_now_is_open_despite_a_cached_failure():
    await _fail_once_then_cache()
    lt = AsyncMock(return_value=_TOOLS)
    with patch("app.modules.mcp_client.list_tools", new=lt), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=_policy_reply())):
        assert await sr.channel_state(None) == ("open", "pve-runner")
    assert lt.await_args.kwargs.get("use_cache") is False
    # and the fresh answer is what the per-step `channel()` reads next
    assert sw.cached_policy(_Spec())[1]["allow"] == ["ANY"]


@pytest.mark.asyncio
async def test_a_runner_still_down_is_unreachable_and_says_why():
    """§17.2019 — one fresh probe and no wait: a runner that comes up a moment later restarts the job by itself
    (`resume_when_runner_answers`, its own file's subject)."""
    await _fail_once_then_cache()
    err = McpError("mcp server 'pve-runner': list_tools failed: ConnectError: All connection attempts failed")
    lt = AsyncMock(side_effect=err)
    with patch("app.modules.mcp_client.list_tools", new=lt):
        state, why = await sr.channel_state(None)
    assert state == "unreachable"
    assert "ConnectError" in why and "answered with no write policy" not in why
    assert lt.await_count == 1                     # one fresh probe, not the cached failure


@pytest.mark.asyncio
async def test_a_runner_that_answers_with_no_policy_is_not_re_probed():
    """An ANSWER of "no writes" is a reading, not a failure: no fresh probes, and it says so."""
    lt = AsyncMock(return_value=[{"name": "run_readonly"}])
    with patch("app.modules.mcp_client.list_tools", new=lt):
        assert await sr.channel_state(None) == ("unreachable", "pve-runner: it answered with no write policy")
    assert lt.await_count == 1


def test_the_gate_reads_the_runner_before_it_classifies_the_steps():
    src = inspect.getsource(ea)
    gate = src[src.index("§17.624 — hands-on assist gate (A)"):]
    assert gate.index("channel_state(db)") < gate.index("_classify_dag_executability(db, job_id)")


def test_a_step_whose_cached_failure_is_overturned_is_asked_not_run_the_old_way():
    """§17.2017 — the held step now asks only whether a runner is registered, which holds it either way."""
    src = inspect.getsource(ea._hand_back_for_approval)
    assert "_sr.runner_registered(db)" in src and 'if _state != "registered":' in src
