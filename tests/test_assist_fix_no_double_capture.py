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


# ── §17.1136 (ledger D-4) — the same rule for research and decision ────────

def _turn_src() -> str:
    return (ROOT / "app" / "modules" / "assist_turn.py").read_text(encoding="utf-8")


def test_run_step_research_capture_is_gated():
    body = _func_body(AGENT, "async def run_step_research(")
    assert "capture_reply: bool = True" in body
    assert "if capture_reply and" in body, "run_step_research's own capture is not gated on capture_reply"


def test_every_turn_loop_research_call_persists_once_under_the_resolved_key():
    """Each `run_step_research(` in the turn loop passes capture_reply=False and
    the ONE capture that follows uses the helper's resolved node key — the
    None-key-vs-resolved-cursor pair is how the dedupe let two rows through."""
    import re
    src = _turn_src()
    calls = list(re.finditer(r"run_step_research\(", src))
    assert len(calls) >= 3, "the turn loop's research call sites moved — re-check this gate"
    for m in calls:
        window = src[m.start(): m.start() + 900]
        assert "capture_reply=False" in window.split(")", 3)[0] + ")" + window.split(")", 3)[1] if ")" in window else False, \
            f"run_step_research at offset {m.start()} does not pass capture_reply=False"
        after = src[m.end(): m.end() + 1400]
        assert 'node_key=(res or {}).get("node_key") or nk' in after, \
            f"the capture after run_step_research at offset {m.start()} must use the resolved node key"


def test_research_endpoint_relies_on_the_helper_default_capture():
    body = _func_body((ROOT / "app" / "routers" / "assist.py").read_text(encoding="utf-8"), "async def assist_research(")
    assert "run_step_research(" in body
    assert "capture_reply=False" not in body
    assert body.count("capture_assistant_reply(") == 0


def test_submit_path_does_not_recapture_the_deliberation_reply():
    body = _func_body(_turn_src(), "async def _submit(")
    dm_block = body[body.index('decision_message'):][:900]
    assert "capture_assistant_reply(" not in dm_block, \
        "the deliberation reply is persisted by run_step_decision through the submit endpoint — one persist per path"
