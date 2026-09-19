"""§17.1111 (Phase 1 ledger L-3) — adaptive think-off for budget-starving models.

The measured defect: model_general landed exactly on its num_predict in 13 % of
calls with nothing usable (reasoning ate the budget); the existing rescues only
switch think off AFTER the wasted draw, per call. The policy remembers per
model: after N starved draws in a window, callers that leave ``think`` unset
get ``think=False`` on the first draw.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import model_router as mr
from app.utils import think_policy as tp


@pytest.fixture(autouse=True)
def _clean_policy(monkeypatch):
    tp.reset()
    monkeypatch.setattr(tp.settings, "think_off_adaptive_enabled", True, raising=False)
    monkeypatch.setattr(tp.settings, "think_off_after_starved_draws", 2, raising=False)
    monkeypatch.setattr(tp.settings, "think_off_window_minutes", 60, raising=False)
    yield
    tp.reset()


def _starved(model="deepseek-v4-pro:cloud", content="", thinking="", tool_calls=None, done="length"):
    raw = {"done_reason": done, "message": {"content": content, "thinking": thinking}}
    if tool_calls:
        raw["message"]["tool_calls"] = tool_calls
    return MagicMock(success=True, text=content, model=model, raw=raw, tool_calls=[])


# ── the policy itself ────────────────────────────────────────────────────────

def test_flips_after_threshold_and_logs_once(caplog):
    m = "deepseek-v4-pro:cloud"
    assert tp.think_default(m) is None
    with caplog.at_level(logging.INFO, logger="scaffold.think_policy"):
        assert tp.note_starved_draw(m, where="tool_call", budget=2500) is False
        assert tp.think_default(m) is None, "one starved draw is variance, not a policy"
        assert tp.note_starved_draw(m, where="generate", budget=8192) is True
        assert tp.think_default(m) is False
        assert tp.note_starved_draw(m) is False, "already flipped — no second flip"
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("think_policy_flipped" in x for x in msgs) == 1
    assert any("starved_draws=2" in x and "budget=8192" in x for x in msgs)
    st = tp.state()
    assert st["models"][m] == {"starved_draws": 3, "think_off": True}


def test_explicit_think_is_never_overridden():
    m = "x"
    tp.note_starved_draw(m); tp.note_starved_draw(m)
    assert tp.resolve(m, None) is False
    assert tp.resolve(m, True) is True
    assert tp.resolve(m, False) is False
    assert tp.resolve("other-model", None) is None


def test_window_decays_and_restores_the_chain_of_thought(monkeypatch, caplog):
    m = "x"
    t = [1000.0]
    monkeypatch.setattr(tp.time, "monotonic", lambda: t[0])
    tp.note_starved_draw(m); tp.note_starved_draw(m)
    assert tp.think_default(m) is False
    t[0] += 61 * 60
    with caplog.at_level(logging.INFO, logger="scaffold.think_policy"):
        assert tp.think_default(m) is None
    assert any("think_policy_restored" in r.getMessage() for r in caplog.records)
    tp.note_starved_draw(m); tp.note_starved_draw(m)
    assert tp.think_default(m) is False, "and it can flip again after that"


def test_disabled_valve_is_inert(monkeypatch):
    monkeypatch.setattr(tp.settings, "think_off_adaptive_enabled", False, raising=False)
    tp.note_starved_draw("x"); tp.note_starved_draw("x")
    assert tp.think_default("x") is None and tp.resolve("x", None) is None


def test_is_starved_reads_length_stops_only():
    assert tp.is_starved(_starved()) is True                                   # empty
    assert tp.is_starved(_starved(content="partial answer", thinking="…")) is True  # squeezed (§17.877)
    assert tp.is_starved(_starved(content="a complete long answer")) is False  # legit long answer
    assert tp.is_starved(_starved(done="stop")) is False
    assert tp.is_starved(_starved(tool_calls=[{"function": {}}])) is False
    assert tp.is_starved(MagicMock(raw=None)) is False


# ── wiring: tool_call ────────────────────────────────────────────────────────

async def test_tool_call_notes_starvation_and_starts_think_off_once_flipped(monkeypatch):
    monkeypatch.setattr(mr, "_model_for_policy", lambda model, role, overrides: "deepseek-v4-pro:cloud")
    seen: list = []

    async def once(messages, tools, model, *, think=None, **kw):
        seen.append(think)
        # first draw starves, second (rescued think=False) still empty → loop ends
        return _starved()

    monkeypatch.setattr(mr, "_tool_call_once", once)
    monkeypatch.setattr(mr, "read_tool_args", lambda r: None)
    tools = [MagicMock()]
    await mr.tool_call([{"role": "user", "content": "x"}], tools, role="model_general", draws=2)
    # call 1: draw 1 think=None (policy not flipped yet), draw 2 rescued think=False
    assert seen == [None, False]
    assert tp.think_default("deepseek-v4-pro:cloud") is False, "two starved draws → flipped"
    seen.clear()
    await mr.tool_call([{"role": "user", "content": "x"}], tools, role="model_general", draws=2)
    assert seen == [False, False], "call 2: think=False from the FIRST draw — no wasted reasoning draw"


async def test_tool_call_respects_explicit_think(monkeypatch):
    monkeypatch.setattr(mr, "_model_for_policy", lambda model, role, overrides: "m")
    tp.note_starved_draw("m"); tp.note_starved_draw("m")
    seen: list = []

    async def once(messages, tools, model, *, think=None, **kw):
        seen.append(think)
        r = MagicMock(success=True, text="ok", model="m", raw={"done_reason": "stop", "message": {}})
        return r

    monkeypatch.setattr(mr, "_tool_call_once", once)
    monkeypatch.setattr(mr, "read_tool_args", lambda r: {"k": 1})
    await mr.tool_call([{"role": "user", "content": "x"}], [MagicMock()], role="r", think=True)
    assert seen == [True]


# ── wiring: generate / chat ──────────────────────────────────────────────────

async def test_generate_uses_the_policy_default_and_notes_starvation(monkeypatch):
    provider = MagicMock()
    calls: list = []

    async def gen(model, prompt, **kw):
        calls.append(kw.get("think"))
        return _starved(model=model)

    provider.generate = gen
    monkeypatch.setattr(mr, "_resolve_role", lambda role, overrides: ("deepseek-v4-pro:cloud", provider))
    monkeypatch.setattr(mr, "_effective_response_schema", lambda schema, prov: None)
    monkeypatch.setattr(mr, "_record_call", AsyncMock(side_effect=lambda r: r))
    async def _rpc(fn, model=None):
        return await fn()

    monkeypatch.setattr(mr, "_retry_provider_call", _rpc)

    await mr.generate("p", role="model_general", max_tokens=8192)
    await mr.generate("p", role="model_general", max_tokens=8192)
    assert calls == [None, None], "two draws left think to the model and both starved"
    assert tp.think_default("deepseek-v4-pro:cloud") is False
    await mr.generate("p", role="model_general", max_tokens=8192)
    assert calls[-1] is False, "third call: policy sends think=False on the first draw"
    st = tp.state()["models"]["deepseek-v4-pro:cloud"]
    assert st["starved_draws"] == 3 and st["think_off"] is True


async def test_chat_uses_the_policy_default(monkeypatch):
    provider = MagicMock()
    calls: list = []

    async def chat(model, messages, **kw):
        calls.append(kw.get("think"))
        return MagicMock(success=True, text="fine", model=model, raw={"done_reason": "stop", "message": {}})

    provider.chat_completion = chat
    monkeypatch.setattr(mr, "_resolve_role", lambda role, overrides: ("m", provider))
    monkeypatch.setattr(mr, "_effective_response_schema", lambda schema, prov: None)
    monkeypatch.setattr(mr, "_record_call", AsyncMock(side_effect=lambda r: r))
    async def _rpc(fn, model=None):
        return await fn()

    monkeypatch.setattr(mr, "_retry_provider_call", _rpc)
    tp.note_starved_draw("m"); tp.note_starved_draw("m")
    await mr.chat([{"role": "user", "content": "x"}], role="r")
    assert calls == [False]
    await mr.chat([{"role": "user", "content": "x"}], role="r", think=True)
    assert calls[-1] is True
