r"""§17.2016 — while a runner is known down, a held step costs a bounded TCP connect, not an MCP session's timeout.

§17.2015 cut the per-step reading of a runner already found down to one fresh probe. That probe is an MCP session,
whose HTTP client connects with a 10s timeout (`mcp_client._open_session`): a host that drops packets instead of
refusing them (powered off behind a switch, a firewall DROP) still held every step for up to 10s. Inside the down
window the probe is now first a TCP connect to the runner's port, bounded at `DOWN_CONNECT_SECONDS`; only a port that
answers gets the MCP probe, so a runner that is back is still read `open` at once.
"""
from __future__ import annotations

import asyncio
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw
from app.modules import supervised_runs as sr
from app.modules.mcp_client import McpError

_TOOLS = [{"name": "run_readonly"}, {"name": "run_supervised"}, {"name": "write_policy"}]
_DOWN = McpError("mcp server 'pve-runner': list_tools failed: ConnectError: All connection attempts failed")


class _Spec:
    name = "pve-runner"
    transport = "streamable_http"

    def __init__(self, endpoint="http://192.0.2.10:8790/mcp/"):
        self.endpoint = endpoint


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ the pre-check itself


@pytest.mark.asyncio
async def test_a_listening_port_answers():
    srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        assert await sw.port_answers(_Spec(f"http://127.0.0.1:{port}/mcp/"), 2.0) is True
    finally:
        srv.close()
        await srv.wait_closed()


@pytest.mark.asyncio
async def test_a_refusing_port_does_not():
    assert await sw.port_answers(_Spec(f"http://127.0.0.1:{_free_port()}/mcp/"), 2.0) is False


@pytest.mark.asyncio
async def test_a_host_that_drops_the_connect_is_given_up_on_within_the_bound(monkeypatch):
    async def hang(*a, **k):
        await asyncio.sleep(30)
    monkeypatch.setattr(asyncio, "open_connection", hang)
    t0 = time.monotonic()
    assert await sw.port_answers(_Spec(), 0.2) is False
    assert time.monotonic() - t0 < 2.0


@pytest.mark.asyncio
async def test_a_runner_with_no_port_is_not_pre_checked():
    stdio = _Spec()
    stdio.transport = "stdio"
    assert await sw.port_answers(stdio, 2.0) is None
    assert await sw.port_answers(_Spec("not a url"), 2.0) is None


# ------------------------------------------------------------------ inside channel_state


@pytest.fixture
def runner(monkeypatch):
    import app.modules.assist_local_runner as lr
    spec = _Spec()
    monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=spec))
    monkeypatch.setattr(sr, "enabled", lambda: True)
    monkeypatch.setattr(sr.settings, "mcp_tool_enabled", True)

    async def no_sleep(s):
        return None
    monkeypatch.setattr(sr.asyncio, "sleep", no_sleep)
    return spec


async def _read(list_tools):
    r = MagicMock()
    r.structured = None
    r.text = '{"helper": "23", "allow": ["ANY"], "sudo": true}'
    with patch("app.modules.mcp_client.list_tools", new=list_tools), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=r)):
        return await sr.channel_state(None)


@pytest.mark.asyncio
async def test_a_known_down_runner_whose_port_is_silent_gets_no_session(runner, monkeypatch):
    monkeypatch.setattr(sw, "port_answers", AsyncMock(return_value=None))
    await _read(AsyncMock(side_effect=_DOWN))                    # found down after waiting
    monkeypatch.setattr(sw, "port_answers", AsyncMock(return_value=False))
    lt = AsyncMock(side_effect=_DOWN)
    state, why = await _read(lt)
    assert state == "unreachable" and "ConnectError" in why
    assert lt.await_count == 0                                   # no MCP session at all
    assert sw.down_within(runner, sw.DOWN_WINDOW)               # and it is still known down


@pytest.mark.asyncio
async def test_a_known_down_runner_whose_port_answers_is_probed_and_read_open(runner, monkeypatch):
    monkeypatch.setattr(sw, "port_answers", AsyncMock(return_value=None))
    await _read(AsyncMock(side_effect=_DOWN))
    monkeypatch.setattr(sw, "port_answers", AsyncMock(return_value=True))
    lt = AsyncMock(return_value=_TOOLS)
    assert await _read(lt) == ("open", "pve-runner")
    assert lt.await_count == 1


@pytest.mark.asyncio
async def test_the_first_outage_is_pre_checked_too(runner, monkeypatch):
    """§17.2019 — the start of a job reads a first outage with the same bounded connect: no MCP session, no wait."""
    pa = AsyncMock(return_value=False)
    monkeypatch.setattr(sw, "port_answers", pa)
    lt = AsyncMock(side_effect=_DOWN)
    state, why = await _read(lt)
    assert state == "unreachable" and "did not accept a connection within 2s" in why
    assert pa.await_count == 1 and lt.await_count == 0
    assert sw.down_within(runner, sw.DOWN_WINDOW)
