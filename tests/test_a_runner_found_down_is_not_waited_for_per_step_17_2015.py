r"""§17.2015 — a runner found down after waiting is probed once per step, not waited for again.

§17.2014 made `channel_state` re-probe a runner whose cached probe failed -- at once, then after 3s and 8s -- so a
machine still coming up (the engine's own rebuild, a host reboot) is not read as down. But every held step asks
`channel_state` (`_hand_back_for_approval`), so while the Proxmox host is really off (it reboots at 02/10/18 ET and has
died silently before) each step paid ~11s plus three connection attempts. Once a runner has been found down after the
wait, later readings within `_DOWN_WINDOW` probe it fresh ONCE, without pausing: a runner that is back still answers
at once, and one that is still down costs a single probe. The first answered probe clears it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw
from app.modules import supervised_runs as sr
from app.modules.mcp_client import McpError

_TOOLS = [{"name": "run_readonly"}, {"name": "run_supervised"}, {"name": "write_policy"}]
_DOWN = McpError("mcp server 'pve-runner': list_tools failed: ConnectError: All connection attempts failed")


class _Spec:
    name = "pve-runner"
    endpoint = "http://192.0.2.10:8790/mcp/"


def _policy_reply():
    r = MagicMock()
    r.structured = None
    r.text = '{"helper": "23", "allow": ["ANY"], "sudo": true}'
    return r


@pytest.fixture
def pauses(monkeypatch):
    import app.modules.assist_local_runner as lr
    monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=_Spec()))
    monkeypatch.setattr(sr, "enabled", lambda: True)
    monkeypatch.setattr(sr.settings, "mcp_tool_enabled", True)
    seen: list[float] = []

    async def no_sleep(s):
        seen.append(s)
    monkeypatch.setattr(sr.asyncio, "sleep", no_sleep)
    # §17.2016's TCP pre-check is its own file's subject; here the endpoint is TEST-NET, so say "no port to try".
    monkeypatch.setattr(sw, "port_answers", AsyncMock(return_value=None))
    return seen


async def _read(list_tools):
    with patch("app.modules.mcp_client.list_tools", new=list_tools), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=_policy_reply())):
        return await sr.channel_state(None)


@pytest.mark.asyncio
async def test_the_first_reading_waits_and_the_next_probes_once(pauses):
    lt = AsyncMock(side_effect=_DOWN)
    assert (await _read(lt))[0] == "unreachable"            # the cold probe, then the wait: 1 + 3 probes
    assert lt.await_count == 4 and pauses == [0.0, 3.0, 8.0]
    lt.reset_mock(); pauses.clear()
    assert (await _read(lt))[0] == "unreachable"            # a held step a moment later: one fresh probe, no pause
    assert lt.await_count == 1 and pauses == [0.0]
    lt.reset_mock(); pauses.clear()
    assert (await _read(lt))[0] == "unreachable"            # and the next
    assert lt.await_count == 1 and pauses == [0.0]


@pytest.mark.asyncio
async def test_a_runner_back_inside_the_window_is_read_open_at_once(pauses):
    assert (await _read(AsyncMock(side_effect=_DOWN)))[0] == "unreachable"
    pauses.clear()
    assert await _read(AsyncMock(return_value=_TOOLS)) == ("open", "pve-runner")
    assert not sw.down_within(_Spec(), sr._DOWN_WINDOW)     # the answer clears it


@pytest.mark.asyncio
async def test_after_it_answered_a_new_outage_is_waited_for_again(pauses):
    await _read(AsyncMock(side_effect=_DOWN))
    await _read(AsyncMock(return_value=_TOOLS))
    sw._policy_cache.pop("pve-runner", None)                # the good answer's cache expires
    pauses.clear()
    lt = AsyncMock(side_effect=_DOWN)
    await _read(lt)
    assert pauses == [0.0, 3.0, 8.0]


@pytest.mark.asyncio
async def test_outside_the_window_it_is_waited_for_again(pauses, monkeypatch):
    await _read(AsyncMock(side_effect=_DOWN))
    sw._down_at["pve-runner"] -= sr._DOWN_WINDOW + 1
    pauses.clear()
    await _read(AsyncMock(side_effect=_DOWN))
    assert pauses == [0.0, 3.0, 8.0]
