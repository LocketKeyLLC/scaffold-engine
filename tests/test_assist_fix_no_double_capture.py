"""§17.1099 — a composer fix must persist exactly ONE assistant turn.

Live: every fix sent through the message composer appeared twice in the web UI.
`_fix_flow` called `run_step_fix` (which persisted the base fix via the §17.726
capture) and then appended a trailer and persisted AGAIN (§17.873) — two
`assist_turns` rows, 12 ms apart, identical but for the trailer. The `/fix`
endpoint does not go through `_fix_flow`, so it was single.

This gate encodes the one-capture-per-path invariant so it can't regress.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT = (ROOT / "app" / "modules" / "assist_agent.py").read_text(encoding="utf-8")
TURN = (ROOT / "app" / "modules" / "assist_turn.py").read_text(encoding="utf-8")
ROUTER = (ROOT / "app" / "routers" / "assist.py").read_text(encoding="utf-8")


def _func_body(src: str, header: str) -> str:
    i = src.index(header)
    nxt = src.find("\nasync def ", i + 1)
    nxt2 = src.find("\ndef ", i + 1)
    ends = [e for e in (nxt, nxt2) if e != -1]
    return src[i:min(ends)] if ends else src[i:]


def test_run_step_fix_capture_is_gated():
    """run_step_fix persists its own reply only when capture_reply is set — so a
    caller that persists the final copy itself can turn it off."""
    assert "capture_reply: bool = True" in AGENT
    body = _func_body(AGENT, "async def run_step_fix(")
    # the §17.726 capture must be guarded by the flag
    assert "if capture_reply and" in body, "run_step_fix's own capture is not gated on capture_reply"


def test_fix_flow_disables_run_step_fix_capture_and_persists_once():
    """The turn-loop path appends a trailer and persists the augmented copy, so
    it must disable run_step_fix's own capture — exactly one capture remains."""
    body = _func_body(TURN, "async def _fix_flow(")
    call = re.search(r"run_step_fix\(.*?\)", body, re.S)
    assert call and "capture_reply=False" in call.group(0), \
        "_fix_flow must call run_step_fix(capture_reply=False)"
    assert body.count("capture_assistant_reply(") == 1, \
        "_fix_flow must persist the fix exactly once (its own trailer-augmented copy)"


def test_fix_endpoint_relies_on_run_step_fix_default_capture():
    """The /fix endpoint does NOT go through _fix_flow, so it must let
    run_step_fix persist (default capture_reply=True) — and must not persist a
    second time itself."""
    body = _func_body(ROUTER, "async def assist_fix(")
    assert "run_step_fix(" in body
    assert "capture_reply=False" not in body, "the /fix endpoint would then never persist the reply"
    assert body.count("capture_assistant_reply(") == 0, "the /fix endpoint must not double-persist"
