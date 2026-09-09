"""§17.999 — the chat surface must not depend on one specific model.

Measured across 8 candidates on the §17.997 goldens, every alternative to the
pin landed at 7-8/10 while the pin scored 10/10 — which read like a capability
gap. Re-running the exact failing (model, golden) pairs passed them, so the
misses are INTERMITTENT variance, not capability. `run_triage` had no retry, so
that variance reached the operator as "I couldn't reach the planner just now".

With one retry, all six candidates drive the production path at 6/6 good,
0 degraded, 0 apology.
"""
import pytest

from app.native_chat.triage import (
    _TRIAGE_DRAWS, _TRIAGE_REQUIRED_SECTIONS, _has_triage_sections, run_triage,
)

_GOOD = ("**Scope so far:** A backup tool.\n\n**Options:**\n- rsync\n- restic\n\n"
         "**Gaps:**\nWHAT: where do the photos live?\n\n**My pick:** restic.")


class _Resp:
    def __init__(self, text, success=True):
        self.text, self.success = text, success


async def _collect(monkeypatch, replies):
    """Drive run_triage with a scripted sequence of model replies."""
    from app import model_router
    from app.native_chat import triage as tr

    seq = list(replies)

    async def _fake_chat(*a, **kw):
        return _Resp(*seq.pop(0)) if isinstance(seq[0], tuple) else _Resp(seq.pop(0))

    monkeypatch.setattr(model_router, "chat", _fake_chat)
    monkeypatch.setattr(tr.model_router, "chat", _fake_chat, raising=False)
    out = ""
    async for chunk in run_triage([{"role": "user", "content": "back up my photos"}]):
        out += chunk
    return out


def test_the_section_check_requires_all_four_in_order():
    assert _has_triage_sections(_GOOD) is True
    assert _has_triage_sections(_GOOD.replace("**My pick:** restic.", "")) is False
    scrambled = ("**Gaps:** x\n**Scope so far:** y\n**Options:** z\n**My pick:** w")
    assert _has_triage_sections(scrambled) is False, "order is part of the contract"
    assert _has_triage_sections("") is False


def test_the_check_matches_the_prompts_own_header_list():
    """If TRIAGE_SYSTEM_PROMPT's contract changes, this check must change with
    it — production and the `--task triage` gate score the same thing."""
    from app.native_chat.triage import TRIAGE_SYSTEM_PROMPT

    for header in _TRIAGE_REQUIRED_SECTIONS:
        assert header.lower() in TRIAGE_SYSTEM_PROMPT.lower(), header


@pytest.mark.asyncio
async def test_an_empty_first_draw_is_redrawn_not_surfaced(monkeypatch):
    """The §17.997 failure: the model returns success=True with zero characters
    and the operator gets a canned apology after ~90s."""
    out = await _collect(monkeypatch, ["", _GOOD])
    assert "couldn't reach the planner" not in out
    assert _has_triage_sections(out)


@pytest.mark.asyncio
async def test_a_malformed_first_draw_is_redrawn(monkeypatch):
    out = await _collect(monkeypatch, ["Sure! Here are some thoughts on backups.", _GOOD])
    assert _has_triage_sections(out)


@pytest.mark.asyncio
async def test_a_good_first_draw_costs_only_one_call(monkeypatch):
    """The retry must be free in the normal case — this is the interactive
    surface, and a second draw is a second wait."""
    from app import model_router
    from app.native_chat import triage as tr

    calls = {"n": 0}

    async def _fake_chat(*a, **kw):
        calls["n"] += 1
        return _Resp(_GOOD)

    monkeypatch.setattr(model_router, "chat", _fake_chat)
    monkeypatch.setattr(tr.model_router, "chat", _fake_chat, raising=False)
    out = ""
    async for c in run_triage([{"role": "user", "content": "x"}]):
        out += c
    assert calls["n"] == 1
    assert _has_triage_sections(out)


@pytest.mark.asyncio
async def test_a_partial_response_beats_the_apology(monkeypatch):
    """A response merely missing a header is still far more useful than "I
    couldn't reach the planner", so it is held as a floor rather than
    discarded."""
    partial = "**Scope so far:** A backup tool.\n\n**Gaps:**\nWHAT: which photos?"
    out = await _collect(monkeypatch, [partial, partial])
    assert "couldn't reach the planner" not in out
    assert "Scope so far" in out


@pytest.mark.asyncio
async def test_the_apology_survives_only_when_every_draw_is_empty(monkeypatch):
    out = await _collect(monkeypatch, [""] * _TRIAGE_DRAWS)
    assert "couldn't reach the planner" in out
