r"""§17.1369 — an unreachable runner is not a hand-off.

Live, 2026-10-05. The Proxmox host went off the LAN:

    ping 192.168.1.156            -> no reply
    /dev/tcp/192.168.1.156/8790   -> No route to host
    write_policy                  -> McpError("All connection attempts failed")

`channel()` answered `None` — the same answer it gives when no runner is
registered at all — and the hands-on gate did this:

    hands_on_by_text job=… node=ADD132 reason=writes:pct exec 103 …
    hands_on_gate_parked: job=… nonexec=151/169 -> awaiting_assist

**151 of 169 steps reclassified and the whole job handed to the operator to carry
out by hand, because one probe had failed.** The gate's own logic says so: with a
channel, every hands-on step is flipped back to executable; with none, they all
count, and 89% of the plan trips the threshold.

Nothing about the work changed. Only what could be READ changed. That is
§17.1363's shape at the job level — absence of a reading rendered as a conclusion
— and it is the most expensive form of it so far, because the conclusion was
"this is all yours now".

So `channel_state` separates the three situations `channel()` collapses:

    "off"          the valve is off, or MCP is disabled
    "none"         no runner is registered
    "unreachable"  a runner IS registered and could not be reached

and the gate HOLDS on the third: the job goes to `blocked` (§17.1211 — not
terminal, re-enterable) with a `warning` the operator's page already renders,
saying what could not be read and that nothing has been handed over.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from app.modules import supervised_runs as sr


class _Spec:
    name = "pve-runner"
    endpoint = "http://192.168.1.156:8790/mcp/"


def _state(monkeypatch, *, spec=_Spec(), policy=None, raises=None, enabled=True, mcp=True):
    import app.modules.assist_local_runner as lr
    import app.modules.assist_supervised as sw

    async def fake_spec(db):
        return spec

    async def fake_policy(s):
        if raises is not None:
            raise raises
        return policy

    monkeypatch.setattr(lr, "runner_spec", fake_spec)
    monkeypatch.setattr(sw, "write_policy", fake_policy)
    monkeypatch.setattr(sr, "enabled", lambda: enabled)
    monkeypatch.setattr(sr.settings, "mcp_tool_enabled", mcp)


# ------------------------------------------- the three situations, told apart


@pytest.mark.asyncio
async def test_an_open_channel_says_open(monkeypatch):
    _state(monkeypatch, policy={"allow": ["ANY"]})
    assert await sr.channel_state(None) == ("open", "pve-runner")


@pytest.mark.asyncio
async def test_an_unreachable_runner_says_unreachable_and_names_it(monkeypatch):
    """The live case: `write_policy` raises McpError."""
    class McpError(Exception):
        pass
    _state(monkeypatch, raises=McpError("All connection attempts failed"))
    state, why = await sr.channel_state(None)
    assert state == "unreachable"
    assert "pve-runner" in why and "McpError" in why


@pytest.mark.asyncio
async def test_a_runner_that_answers_with_no_policy_is_also_unreachable(monkeypatch):
    _state(monkeypatch, policy={})
    state, why = await sr.channel_state(None)
    assert state == "unreachable" and "no write policy" in why


@pytest.mark.asyncio
async def test_no_runner_registered_is_not_unreachable(monkeypatch):
    """This is the case the hand-off exists for, and it must stay distinguishable."""
    _state(monkeypatch, spec=None)
    assert await sr.channel_state(None) == ("none", "")


@pytest.mark.asyncio
async def test_the_valve_off_is_its_own_answer(monkeypatch):
    _state(monkeypatch, enabled=False)
    assert await sr.channel_state(None) == ("off", "")
    _state(monkeypatch, mcp=False)
    assert await sr.channel_state(None) == ("off", "")


@pytest.mark.asyncio
async def test_a_raise_anywhere_is_unreachable_not_absent(monkeypatch):
    import app.modules.assist_local_runner as lr

    async def boom(db):
        raise RuntimeError("db gone")
    monkeypatch.setattr(lr, "runner_spec", boom)
    monkeypatch.setattr(sr, "enabled", lambda: True)
    monkeypatch.setattr(sr.settings, "mcp_tool_enabled", True)
    state, why = await sr.channel_state(None)
    assert state == "unreachable" and "RuntimeError" in why


# ------------------------------------------------------------- the gate


def _gate_source() -> str:
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("if settings.hands_on_assist_gate_enabled:")
    return src[i:i + 4200]


def test_the_gate_asks_why_there_is_no_channel():
    body = _gate_source()
    assert "_sr_chan.channel_state(db)" in body
    assert "from app.modules import supervised_runs as _sr_chan" in body
    assert '_ch_state == "unreachable"' in body
    # and it asks BEFORE it decides to park
    assert body.index("channel_state(db)") < body.index('_cls["hands_on"] and')


def test_the_gate_holds_instead_of_handing_over():
    body = _gate_source()
    assert "hands_on_gate_held" in body
    assert "_park_job_awaiting_assist" in body          # the other path still exists
    held = body[body.index('_ch_state == "unreachable"'):body.index("_park_job_awaiting_assist")]
    assert "awaiting_assist" not in held, "the held path must not hand the plan over"


def test_the_job_goes_to_blocked_which_is_re_enterable():
    """§17.1119 — through `job_state.transition()`, not a raw UPDATE: the
    raw-site ratchet in tests/test_job_state.py refused the first cut."""
    body = _gate_source()
    assert 'to="blocked"' in body
    assert '_js.transition(' in body
    assert 'expected_from=("running", "executing")' in body
    assert "UPDATE jobs SET status" not in body


def test_the_operator_is_told_what_could_not_be_read():
    body = _gate_source()
    assert '_sse("warning"' in body
    assert "could not be reached" in body
    assert "has NOT handed" in body and "nothing about the work changed" in body
    assert "press Run again once the machine" in body


def test_the_event_name_is_one_the_page_renders():
    """`blocked` is declared legacy in the SSE inventory and the SPA renders no
    such event, so emitting it would say this to nobody."""
    body = _gate_source()
    assert '_sse("blocked"' not in body
    js = (pathlib.Path(__file__).resolve().parent.parent
          / "app/ui/static/views/theater.js").read_text()
    assert '"warning"' in js


def test_a_registered_runner_that_is_reachable_still_hands_off():
    """The gate is narrowed, not removed: a plan that really is hands-on, with a
    runner that answers, still goes to assist."""
    body = _gate_source()
    i = body.index('_cls["hands_on"] and _ch_state == "unreachable"')
    j = body.index('if _cls["hands_on"]:', i)
    assert j > i, "the unconditional hand-off must still follow"
    assert "_park_job_awaiting_assist(db, job_id, _cls)" in body[j:]


def test_the_classifier_still_flips_hands_on_steps_when_a_channel_exists():
    """Why one failed probe reclassified 151 steps: with a channel every hands-on
    step is flipped back to executable, and with none they all count."""
    from app.modules.execution_agent import _classify_dag_executability
    src = inspect.getsource(_classify_dag_executability)
    assert re.search(r"if on and _ch is not None and not why\.startswith\(\"tool:human\"\)", src)
    assert "nonexec > total * settings.hands_on_assist_gate_threshold" in src
