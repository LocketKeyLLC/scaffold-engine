"""§17.452 (Phase C) — Chain-of-Verification revision pass + wire-in."""
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest

from app.modules import cove as C
from app.modules import research_agent as RA
from app.modules.research_state import ResearchState


def _q_resp(questions, success=True):
    return SimpleNamespace(
        success=success,
        tool_calls=[SimpleNamespace(arguments={"questions": questions})],
        text="",
    )


def _gen(text, success=True):
    return SimpleNamespace(success=success, text=text)


# ───────────────────────────── cove_revise ─────────────────────────────

@pytest.mark.asyncio
async def test_cove_revises_three_step():
    with patch.object(C.model_router, "tool_call",
                      new=AsyncMock(return_value=_q_resp(["Q1?", "Q2?"]))), \
         patch.object(C.model_router, "generate",
                      new=AsyncMock(side_effect=[_gen("A1; A2"), _gen("Revised text.")])):
        out = await C.cove_revise("Original summary.", "context text")
    assert out["revised"] == "Revised text."
    assert out["questions"] == ["Q1?", "Q2?"]
    assert out["changed"] is True


@pytest.mark.asyncio
async def test_cove_changed_false_when_identical():
    with patch.object(C.model_router, "tool_call",
                      new=AsyncMock(return_value=_q_resp(["Q1?"]))), \
         patch.object(C.model_router, "generate",
                      new=AsyncMock(side_effect=[_gen("A1"), _gen("Same text.")])):
        out = await C.cove_revise("Same text.", "ctx")
    assert out["changed"] is False


@pytest.mark.asyncio
async def test_cove_none_on_empty_input():
    assert await C.cove_revise("", "ctx") is None
    assert await C.cove_revise("ans", "  ") is None


@pytest.mark.asyncio
async def test_cove_none_on_no_questions():
    with patch.object(C.model_router, "tool_call",
                      new=AsyncMock(return_value=_q_resp([]))):
        assert await C.cove_revise("ans", "ctx") is None


@pytest.mark.asyncio
async def test_cove_none_on_answer_step_failure():
    with patch.object(C.model_router, "tool_call",
                      new=AsyncMock(return_value=_q_resp(["Q?"]))), \
         patch.object(C.model_router, "generate",
                      new=AsyncMock(return_value=_gen("", success=False))):
        assert await C.cove_revise("ans", "ctx") is None


@pytest.mark.asyncio
async def test_cove_none_on_exception_failsoft():
    with patch.object(C.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        assert await C.cove_revise("ans", "ctx") is None


def test_questions_tool_is_proper_Tool_instance():
    # §17.449 regression — tool_call needs a Tool object, not a raw dict.
    from app.providers.base import Tool
    assert isinstance(C._QUESTIONS_TOOL, Tool)
    assert C._QUESTIONS_TOOL.name == "list_verification_questions"


# ───────────────────────────── wire-in ─────────────────────────────

@pytest.mark.asyncio
async def test_maybe_cove_off_by_default():
    state = ResearchState(topic="t")
    state.all_entries = [{"source": "a", "content": "x"}]
    out = await RA._maybe_cove_revise("orig", state, None)
    assert out == "orig" and state.cove is None


@pytest.mark.asyncio
async def test_maybe_cove_revises_when_enabled(monkeypatch):
    state = ResearchState(topic="t")
    state.all_entries = [{"source": "a", "content": "x", "facet": "f"}]
    monkeypatch.setattr(RA.settings, "cove_check_enabled", True)
    with patch("app.modules.cove.cove_revise",
               new=AsyncMock(return_value={"revised": "better",
                                           "questions": ["q1", "q2"], "changed": True})):
        out = await RA._maybe_cove_revise("orig", state, None)
    assert out == "better"
    assert state.cove == {"changed": True, "questions": 2}


def test_finalize_adds_cove_note():
    state = ResearchState(topic="t")
    state.cove = {"changed": True, "questions": 3}
    out = RA._finalize_summary_text("Summary.", state)
    assert "Chain-of-Verification: revised after 3" in out


def test_finalize_no_cove_note_when_unchanged():
    state = ResearchState(topic="t")
    state.cove = {"changed": False, "questions": 3}
    out = RA._finalize_summary_text("Summary.", state)
    assert "Chain-of-Verification" not in out


def test_payload_carries_cove():
    state = ResearchState(topic="t")
    state.cove = {"changed": True, "questions": 3}
    payload = RA._build_research_complete_payload(
        state, "s", mode="topic", duration_ms=1, summary="x")
    assert payload["cove"]["changed"] is True


def test_payload_cove_none_when_unrun():
    state = ResearchState(topic="t")
    payload = RA._build_research_complete_payload(
        state, "s", mode="topic", duration_ms=1, summary="x")
    assert payload["cove"] is None
