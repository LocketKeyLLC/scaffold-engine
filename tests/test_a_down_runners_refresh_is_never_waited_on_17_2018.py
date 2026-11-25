r"""§17.2018 — a down runner's cache refresh runs in the background; no caller waits on it.

After §17.2017 a held step no longer reads the runner, but `channel()` -- which it still calls first -- refreshes the
cached policy once its 60s entry expires, and for a runner found down that refresh was a 2s port check paid in-line
by whichever step came next. Now a cache miss for a runner found down returns the last failure at once and starts ONE
background probe (`use_cache=False`, so §17.2017's port check runs there). A runner that is back reads open as soon as
that probe answers. And since held steps no longer go through `channel_state`, a probe that still fails renews the
down mark itself -- otherwise the window would lapse after 120s and the refresh would go back to a 10s MCP connect.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw

_TOOLS = [{"name": "run_readonly"}, {"name": "run_supervised"}, {"name": "write_policy"}]


class _Spec:
    name = "pve-runner"
    transport = "streamable_http"
    endpoint = "http://192.0.2.10:8790/mcp/"


def _known_down():
    sw._policy_error["pve-runner"] = "ConnectError"
    sw.mark_down(_Spec())
    # the 60s cached failure has expired: no entry


async def _settle():
    t = sw._refreshing.get("pve-runner")
    if t is not None:
        await t


def _reply():
    r = MagicMock()
    r.structured = None
    r.text = '{"helper": "23", "allow": ["ANY"], "sudo": true}'
    return r


@pytest.mark.asyncio
async def test_a_cache_miss_for_a_down_runner_returns_at_once():
    _known_down()

    async def slow_port(spec, seconds):
        await asyncio.sleep(0.5)
        return False
    pa = AsyncMock(side_effect=slow_port)
    with patch.object(sw, "port_answers", new=pa):
        t0 = time.monotonic()
        assert await sw.write_policy(_Spec()) is None
        assert time.monotonic() - t0 < 0.1                   # the caller did not wait for the port
        assert pa.await_count <= 1
        await _settle()
    assert pa.await_count == 1                               # the background probe ran it
    assert sw.cached_policy(_Spec()) == (True, None)


@pytest.mark.asyncio
async def test_many_callers_start_one_probe():
    _known_down()
    pa = AsyncMock(return_value=False)
    with patch.object(sw, "port_answers", new=pa):
        for _ in range(5):
            sw._policy_cache.pop("pve-runner", None)         # every caller misses the cache
            assert await sw.write_policy(_Spec()) is None
        await _settle()
    assert pa.await_count == 1


@pytest.mark.asyncio
async def test_a_runner_that_is_back_is_read_open_once_the_probe_answers():
    _known_down()
    with patch.object(sw, "port_answers", new=AsyncMock(return_value=True)), \
         patch("app.modules.mcp_client.list_tools", new=AsyncMock(return_value=_TOOLS)), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=_reply())):
        assert await sw.write_policy(_Spec()) is None        # this caller: the last reading
        await _settle()
        assert (await sw.write_policy(_Spec()))["allow"] == ["ANY"]   # the next: open
    assert not sw.down_within(_Spec(), sw.DOWN_WINDOW)


@pytest.mark.asyncio
async def test_a_probe_that_still_fails_keeps_the_runner_known_down():
    _known_down()
    sw._down_at["pve-runner"] -= sw.DOWN_WINDOW - 1          # one second of the window left
    with patch.object(sw, "port_answers", new=AsyncMock(return_value=False)):
        await sw.write_policy(_Spec(), use_cache=False)
    assert sw.down_within(_Spec(), 10.0)                     # renewed, not about to lapse


@pytest.mark.asyncio
async def test_a_runner_not_known_down_is_probed_in_line_as_before():
    lt = AsyncMock(return_value=_TOOLS)
    with patch("app.modules.mcp_client.list_tools", new=lt), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=_reply())):
        assert (await sw.write_policy(_Spec()))["allow"] == ["ANY"]
    assert lt.await_count == 1 and not sw._refreshing
