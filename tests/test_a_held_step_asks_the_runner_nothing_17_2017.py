r"""§17.2017 — a held step asks whether a runner is registered, not whether it answers.

After §17.2014–2016, every hands-on step the loop claimed while the Proxmox host was down still paid a reading of the
runner in `_hand_back_for_approval` -- at best a 2s TCP connect (§17.2016), 10s before it. But that function holds
the step for ANY registered runner, open or unreachable (§17.2014 made "open" hold too), so the reading decided
nothing: only "off"/"none" take the ordinary path. It now asks `supervised_runs.runner_registered`, which touches no
network. And the cache refresh that `channel()` still makes once a minute goes through `write_policy`, which, for a
runner found down, tries the port (bounded) before opening an MCP session.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw
from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr

NODE = {"id": "00000000-0000-0000-0000-000000000001", "node_key": "M1", "title": "Fetch Sonarr series details"}


class _Spec:
    name = "pve-runner"
    transport = "streamable_http"
    endpoint = "http://192.0.2.10:8790/mcp/"


def _db():
    db = AsyncMock()

    async def execute(stmt, params=None):
        r = MagicMock()
        r.scalar = MagicMock(return_value={})
        return r
    db.execute = execute
    db.commit = AsyncMock()
    return db


def _no_network():
    """Fakes that COUNT: `write_policy` swallows whatever its probe raises, so a raising fake would pass silently."""
    lt = AsyncMock(return_value=[])
    pa = AsyncMock(return_value=False)
    return lt, pa, (patch("app.modules.mcp_client.list_tools", new=lt), patch.object(sw, "port_answers", new=pa))


@pytest.fixture
def registered(monkeypatch):
    import app.modules.assist_local_runner as lr
    monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=_Spec()))
    monkeypatch.setattr(sr, "enabled", lambda: True)
    monkeypatch.setattr(sr.settings, "mcp_tool_enabled", True)


@pytest.mark.asyncio
async def test_a_held_step_with_a_known_down_runner_reaches_no_network(registered):
    sw._policy_cache["pve-runner"] = (time.monotonic() + 60.0, None)     # the cached failure `channel()` reads
    sw._policy_error["pve-runner"] = "ConnectError"
    sw.mark_down(_Spec())
    lt, pa, (p1, p2) = _no_network()
    with p1, p2:
        out = await ea._hand_back_for_approval(_db(), "j", NODE, "Shell")
    assert out and out["status"] == "needs_approval"
    assert lt.await_count == 0 and pa.await_count == 0


@pytest.mark.asyncio
async def test_runner_registered_says_what_is_there_without_asking(registered, monkeypatch):
    lt, pa, (p1, p2) = _no_network()
    with p1, p2:
        assert await sr.runner_registered(None) == ("registered", "pve-runner")
        import app.modules.assist_local_runner as lr
        monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=None))
        assert await sr.runner_registered(None) == ("none", "")
        monkeypatch.setattr(sr, "enabled", lambda: False)
        assert await sr.runner_registered(None) == ("off", "")
    assert lt.await_count == 0 and pa.await_count == 0


@pytest.mark.asyncio
async def test_the_cache_refresh_for_a_down_runner_tries_the_port_first():
    sw.mark_down(_Spec())
    sw._policy_error["pve-runner"] = "ConnectError"
    lt = AsyncMock(side_effect=AssertionError("no MCP session for a silent port"))
    with patch("app.modules.mcp_client.list_tools", new=lt), \
         patch.object(sw, "port_answers", new=AsyncMock(return_value=False)) as pa:
        # the probe itself; since §17.2018 a cache miss for a down runner runs it in the background
        assert await sw.write_policy(_Spec(), use_cache=False) is None
    assert pa.await_args.args[1] == sw.DOWN_CONNECT_SECONDS
    assert sw.cached_policy(_Spec()) == (True, None)          # and the reading is cached like any failure


@pytest.mark.asyncio
async def test_a_port_that_answers_gets_the_session():
    sw.mark_down(_Spec())
    lt = AsyncMock(return_value=[{"name": "run_readonly"}])
    with patch("app.modules.mcp_client.list_tools", new=lt), \
         patch.object(sw, "port_answers", new=AsyncMock(return_value=True)):
        await sw.write_policy(_Spec())
    assert lt.await_count == 1 and not sw.down_within(_Spec(), sw.DOWN_WINDOW)


@pytest.mark.asyncio
async def test_a_runner_not_known_down_is_not_port_checked():
    pa = AsyncMock(return_value=False)
    with patch("app.modules.mcp_client.list_tools", new=AsyncMock(return_value=[{"name": "run_readonly"}])), \
         patch.object(sw, "port_answers", new=pa):
        await sw.write_policy(_Spec())
    assert pa.await_count == 0
