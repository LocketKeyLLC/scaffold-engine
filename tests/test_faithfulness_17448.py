"""§17.448 (Phase B / B1) — RAGAS-inspired faithfulness scoring + wire-in."""
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest

from app.modules import faithfulness as F
from app.modules import research_agent as RA
from app.modules.research_state import ResearchState


def _resp(claims, success=True):
    return SimpleNamespace(
        success=success,
        tool_calls=[SimpleNamespace(arguments={"claims": claims})],
        text="",
    )


# ───────────────────────── score_faithfulness ─────────────────────────

@pytest.mark.asyncio
async def test_score_basic_ratio():
    claims = [
        {"claim": "a", "supported": True},
        {"claim": "b", "supported": True},
        {"claim": "c", "supported": False},
    ]
    with patch.object(F.model_router, "tool_call", new=AsyncMock(return_value=_resp(claims))):
        out = await F.score_faithfulness("answer text", "context text")
    assert out["total"] == 3 and out["supported"] == 2
    assert out["score"] == 0.67
    assert out["unsupported_claims"] == ["c"]


@pytest.mark.asyncio
async def test_none_on_empty_input():
    assert await F.score_faithfulness("", "ctx") is None
    assert await F.score_faithfulness("ans", "  ") is None


@pytest.mark.asyncio
async def test_none_on_llm_failure():
    with patch.object(F.model_router, "tool_call", new=AsyncMock(return_value=_resp([], success=False))):
        assert await F.score_faithfulness("ans", "ctx") is None


@pytest.mark.asyncio
async def test_none_on_no_claims():
    with patch.object(F.model_router, "tool_call", new=AsyncMock(return_value=_resp([]))):
        assert await F.score_faithfulness("ans", "ctx") is None


@pytest.mark.asyncio
async def test_none_on_exception_failsoft():
    with patch.object(F.model_router, "tool_call", new=AsyncMock(side_effect=RuntimeError("boom"))):
        assert await F.score_faithfulness("ans", "ctx") is None


# ───────────────────────── wire-in (research_agent) ─────────────────────────

def test_finalize_summary_appends_faithfulness_note():
    state = ResearchState(topic="t")
    state.all_entries = [{"source": "http://a", "content": "x", "facet": "f"}]
    state.faithfulness = {"score": 0.85, "supported": 17, "total": 20, "unsupported_claims": []}
    out = RA._finalize_summary_text("Summary.", state)
    assert "Faithfulness: 0.85" in out and "17/20" in out


def test_finalize_summary_no_note_when_unscored():
    state = ResearchState(topic="t")
    out = RA._finalize_summary_text("Summary.", state)
    assert "Faithfulness" not in out


@pytest.mark.asyncio
async def test_maybe_score_off_by_default():
    state = ResearchState(topic="t")
    state.all_entries = [{"source": "a", "content": "x"}]
    # settings.faithfulness_check_enabled defaults False
    assert await RA._maybe_score_faithfulness("s", state, None) is None


@pytest.mark.asyncio
async def test_maybe_score_runs_when_enabled(monkeypatch):
    state = ResearchState(topic="t")
    state.all_entries = [{"source": "a", "content": "x", "facet": "f"}]
    monkeypatch.setattr(RA.settings, "faithfulness_check_enabled", True)
    with patch("app.modules.faithfulness.score_faithfulness",
               new=AsyncMock(return_value={"score": 0.5, "supported": 1, "total": 2})):
        out = await RA._maybe_score_faithfulness("summary", state, None)
    assert out["score"] == 0.5


def test_complete_payload_carries_faithfulness():
    state = ResearchState(topic="t")
    state.faithfulness = {"score": 0.9, "supported": 9, "total": 10}
    payload = RA._build_research_complete_payload(
        state, "sess", mode="topic", duration_ms=1, summary="s")
    assert payload["faithfulness"]["score"] == 0.9


def test_complete_payload_faithfulness_none_when_unscored():
    state = ResearchState(topic="t")
    payload = RA._build_research_complete_payload(
        state, "sess", mode="topic", duration_ms=1, summary="s")
    assert payload["faithfulness"] is None
