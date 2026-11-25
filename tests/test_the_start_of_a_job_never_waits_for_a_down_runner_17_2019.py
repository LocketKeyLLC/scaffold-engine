r"""§17.2019 — the start of a job never waits for a down runner; the engine restarts the job when it answers.

§17.2014 gave a runner whose probe failed 3s and then 8s more before the hands-on gate read it unreachable, so a machine
still coming up (the engine's own rebuild, a host reboot) did not block the job. A runner that was really off cost every
job start those 11s plus three more probes -- each up to an MCP session's 10s connect on a host that drops packets.
Now the gate reads the runner once (a bounded TCP connect first), blocks at once, and `resume_when_runner_answers`
starts the job again when the runner answers: the machine coming up is still not a failure, and nobody waits on it.
"""
from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw
from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr
from app.modules.mcp_client import McpError

_DOWN = McpError("mcp server 'pve-runner': list_tools failed: ConnectError: All connection attempts failed")
_JOB = "0fb4ea7f-6d24-4b70-b3db-a3ea244c7a4d"


class _Spec:
    name = "pve-runner"
    transport = "streamable_http"
    endpoint = "http://192.0.2.10:8790/mcp/"


@pytest.fixture
def runner(monkeypatch):
    import app.modules.assist_local_runner as lr
    monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=_Spec()))
    monkeypatch.setattr(sr, "enabled", lambda: True)
    monkeypatch.setattr(sr.settings, "mcp_tool_enabled", True)
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def no_sleep(s):
        slept.append(s)
        await real_sleep(0)
    monkeypatch.setattr(sr.asyncio, "sleep", no_sleep)
    return slept


# ------------------------------------------------------------------ the reading


@pytest.mark.asyncio
async def test_a_runner_that_drops_the_connect_costs_one_bounded_connect_and_no_wait(runner, monkeypatch):
    pa = AsyncMock(return_value=False)
    monkeypatch.setattr(sw, "port_answers", pa)
    lt = AsyncMock(side_effect=_DOWN)
    with patch("app.modules.mcp_client.list_tools", new=lt):
        state, _why = await sr.channel_state(None)
    assert state == "unreachable"
    assert pa.await_count == 1 and pa.await_args.args[1] == sw.DOWN_CONNECT_SECONDS
    assert lt.await_count == 0 and runner == []


@pytest.mark.asyncio
async def test_a_runner_whose_port_answers_but_whose_probe_fails_is_probed_once(runner, monkeypatch):
    monkeypatch.setattr(sw, "port_answers", AsyncMock(return_value=True))
    lt = AsyncMock(side_effect=_DOWN)
    with patch("app.modules.mcp_client.list_tools", new=lt):
        assert (await sr.channel_state(None))[0] == "unreachable"
    assert lt.await_count == 1 and runner == []


# ------------------------------------------------------------------ the restart


class _Db:
    """`async_session()` stand-in: each `SELECT updated_at` answers the next stamp."""

    def __init__(self, stamps):
        self.stamps = list(stamps)

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a, **_k):
        r = MagicMock()
        r.scalar.return_value = self.stamps.pop(0) if len(self.stamps) > 1 else self.stamps[0]
        return r


async def _watch(monkeypatch, *, stamps, states):
    import app.database as dbm
    import app.modules.profiles as profiles
    import app.modules.run_broker as rb
    monkeypatch.setattr(dbm, "async_session", _Db(stamps))
    monkeypatch.setattr(sr, "RESUME_POLL_SECONDS", 0.0)
    monkeypatch.setattr(sr, "channel_state", AsyncMock(side_effect=states))
    monkeypatch.setattr(profiles, "resolve_job_overrides", AsyncMock(return_value=None))
    start = MagicMock()
    monkeypatch.setattr(rb, "start", start)
    sr.resume_when_runner_answers(_JOB)
    await sr._resuming[_JOB]
    return start


@pytest.mark.asyncio
async def test_a_job_blocked_on_a_down_runner_starts_itself_when_the_runner_answers(runner, monkeypatch):
    start = await _watch(monkeypatch, stamps=["t0"],
                         states=[("unreachable", "x"), ("unreachable", "x"), ("open", "pve-runner")])
    assert start.call_count == 1 and start.call_args.args[0] == _JOB
    assert sr.channel_state.await_count == 3
    assert _JOB not in sr._resuming


@pytest.mark.asyncio
async def test_a_job_someone_touched_since_is_left_alone(runner, monkeypatch):
    """Cancelled, re-run or edited while blocked: `updated_at` moved, so the watcher stops without starting it."""
    start = await _watch(monkeypatch, stamps=["t0", "t1"], states=[("open", "pve-runner")])
    assert start.call_count == 0


@pytest.mark.asyncio
async def test_a_job_no_longer_blocked_is_not_watched(runner, monkeypatch):
    start = await _watch(monkeypatch, stamps=[None], states=[("open", "pve-runner")])
    assert start.call_count == 0 and sr.channel_state.await_count == 0


@pytest.mark.asyncio
async def test_one_watcher_per_job(runner, monkeypatch):
    import app.database as dbm
    gate = asyncio.Event()

    async def held(*_a, **_k):
        await gate.wait()
        return ("off", "")
    monkeypatch.setattr(dbm, "async_session", _Db(["t0"]))
    monkeypatch.setattr(sr, "RESUME_POLL_SECONDS", 0.0)
    cs = AsyncMock(side_effect=held)
    monkeypatch.setattr(sr, "channel_state", cs)
    sr.resume_when_runner_answers(_JOB)
    first = sr._resuming[_JOB]
    sr.resume_when_runner_answers(_JOB)
    assert sr._resuming[_JOB] is first
    gate.set()
    await first


def test_the_gate_starts_the_watcher_when_it_blocks_and_says_so():
    src = inspect.getsource(ea)
    gate = src[src.index("§17.624 — hands-on assist gate (A)"):]
    blocked = gate.index('reason="hands_on_gate:runner_unreachable"')
    assert blocked < gate.index("_sr_chan.resume_when_runner_answers(job_id)") < gate.index('_sse("warning"')
    assert "starts it again by itself" in gate


def test_the_reading_has_no_wait_in_it():
    assert "sleep" not in inspect.getsource(sr.channel_state)
