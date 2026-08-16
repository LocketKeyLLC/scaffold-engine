"""§17.799 — flag-gated cite-aware research summary + per-citation scoring wire-in."""
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest

from app.modules import research_agent as RA
from app.modules.research_state import ResearchState


def _state_with_entries():
    s = ResearchState(topic="vector databases")
    s.all_entries = [
        {"source": "http://a", "content": "Alpha content about ANN indexes.",
         "confidence_score": 0.9, "source_type": "web", "facet": "f1"},
        {"source": "http://b", "content": "Beta content about quantization.",
         "confidence_score": 0.7, "source_type": "web", "facet": "f2"},
        {"source": "http://a", "content": "duplicate low-confidence alpha",
         "confidence_score": 0.3, "source_type": "web", "facet": "f1"},
    ]
    return s


# ───────────────────── pure helpers ─────────────────────

def test_numbered_sources_dedupe_rank_truncate():
    srcs = RA._build_numbered_summary_sources(_state_with_entries())
    # deduped by URL (kept the 0.9 alpha, not the 0.3 dup) and confidence-ranked
    assert [s["url"] for s in srcs] == ["http://a", "http://b"]
    assert srcs[0]["confidence_score"] == 0.9
    assert all(len(s["text"]) <= RA._CITE_SUMMARY_SRC_CHARS for s in srcs)


def test_numbered_sources_skips_entries_without_url_or_content():
    s = ResearchState(topic="t")
    s.all_entries = [
        {"source": "", "content": "no url", "confidence_score": 0.9},
        {"source": "http://x", "content": "", "confidence_score": 0.9},
        {"source": "http://y", "content": "real", "confidence_score": 0.5},
    ]
    srcs = RA._build_numbered_summary_sources(s)
    assert [x["url"] for x in srcs] == ["http://y"]


def test_cite_prompt_body_is_numbered():
    srcs = RA._build_numbered_summary_sources(_state_with_entries())
    body = RA._build_cite_summary_prompt_body(srcs)
    assert body.startswith("[1] Alpha content")
    assert "[2] Beta content" in body


# ───────────────────── finalize note ─────────────────────

def test_finalize_appends_citation_note():
    state = ResearchState(topic="t")
    state.citation_faithfulness = {"score": 0.8, "supported": 4, "total": 5, "dangling": 0}
    out = RA._finalize_summary_text("Summary [1].", state)
    assert "Citation faithfulness: 0.80" in out and "4/5" in out


def test_finalize_citation_note_flags_dangling():
    state = ResearchState(topic="t")
    state.citation_faithfulness = {"score": 0.5, "supported": 1, "total": 2, "dangling": 1}
    out = RA._finalize_summary_text("Summary [9].", state)
    assert "1 dangling" in out


def test_finalize_no_citation_note_when_unscored():
    out = RA._finalize_summary_text("Summary.", ResearchState(topic="t"))
    assert "Citation faithfulness" not in out


# ───────────────────── scorer gate ─────────────────────

@pytest.mark.asyncio
async def test_maybe_score_off_by_default(monkeypatch):
    srcs = RA._build_numbered_summary_sources(_state_with_entries())
    # Code default is False, but `make test` runs INSIDE the live orchestrator,
    # whose compose env sets CITATION_FAITHFULNESS_CHECK_ENABLED=true (§17.798–800
    # "ON in live compose"). Force the default explicitly so the test is hermetic
    # (mirrors the sibling test_maybe_score_runs_when_enabled, which forces True).
    monkeypatch.setattr(RA.settings, "citation_faithfulness_check_enabled", False)
    assert await RA._maybe_score_citation_faithfulness("s [1].", srcs, None) is None


@pytest.mark.asyncio
async def test_maybe_score_runs_when_enabled(monkeypatch):
    srcs = RA._build_numbered_summary_sources(_state_with_entries())
    monkeypatch.setattr(RA.settings, "citation_faithfulness_check_enabled", True)
    with patch("app.modules.citation_faithfulness.score_citation_faithfulness",
               new=AsyncMock(return_value={"score": 0.75, "supported": 3, "total": 4,
                                           "cited": 4, "dangling": 0,
                                           "unsupported_citations": []})):
        out = await RA._maybe_score_citation_faithfulness("summary [1].", srcs, None)
    assert out["score"] == 0.75


# ───────────────────── payload ─────────────────────

def test_payload_carries_citation_faithfulness():
    state = ResearchState(topic="t")
    state.citation_faithfulness = {"score": 0.9, "supported": 9, "total": 10}
    payload = RA._build_research_complete_payload(
        state, "sess", mode="topic", duration_ms=1, summary="s")
    assert payload["citation_faithfulness"]["score"] == 0.9


def test_payload_citation_faithfulness_none_when_unscored():
    payload = RA._build_research_complete_payload(
        ResearchState(topic="t"), "sess", mode="topic", duration_ms=1, summary="s")
    assert payload["citation_faithfulness"] is None


# ───────────────────── _generate_summary end-to-end (mocked LLM) ─────────────────────

@pytest.mark.asyncio
async def test_generate_summary_default_path_uses_v1_prompt_no_citation_score(monkeypatch):
    """Flag OFF → normal SUMMARY_SYSTEM_V1 path; citation_faithfulness stays None."""
    state = _state_with_entries()
    # Force the code default off — the live-compose env has the valve ON, so a
    # bare test would take the cite-aware path and stamp a score (§17.807 baseline).
    monkeypatch.setattr(RA.settings, "citation_faithfulness_check_enabled", False)
    monkeypatch.setattr(RA, "_generate_options", AsyncMock(return_value=None))
    captured = {}

    async def _fake_generate(prompt, *, role, overrides, system, temperature, max_tokens):
        captured["system"] = system
        return SimpleNamespace(success=True, text="Plain summary without markers.")

    with patch.object(RA.model_router, "generate", new=AsyncMock(side_effect=_fake_generate)):
        out = await RA._generate_summary(state)
    assert captured["system"] is RA.SUMMARY_SYSTEM_V1
    assert state.citation_faithfulness is None
    assert "Citation faithfulness" not in out


@pytest.mark.asyncio
async def test_generate_summary_cite_mode_uses_cite_prompt_and_scores(monkeypatch):
    """Flag ON → cite-aware prompt + numbered body; state.citation_faithfulness stamped."""
    state = _state_with_entries()
    monkeypatch.setattr(RA.settings, "citation_faithfulness_check_enabled", True)
    monkeypatch.setattr(RA, "_generate_options", AsyncMock(return_value=None))
    captured = {}

    async def _fake_generate(prompt, *, role, overrides, system, temperature, max_tokens):
        captured["system"] = system
        captured["prompt"] = prompt
        return SimpleNamespace(success=True, text="ANN indexes speed search [1].")

    scored = {"score": 1.0, "supported": 1, "total": 1, "cited": 1, "dangling": 0,
              "unsupported_citations": []}
    with patch.object(RA.model_router, "generate", new=AsyncMock(side_effect=_fake_generate)), \
         patch("app.modules.citation_faithfulness.score_citation_faithfulness",
               new=AsyncMock(return_value=scored)):
        out = await RA._generate_summary(state)

    assert captured["system"] is RA._CITE_SUMMARY_SYSTEM
    assert "[1] Alpha content" in captured["prompt"]  # numbered sources in the prompt
    assert state.citation_faithfulness["score"] == 1.0
    assert "Citation faithfulness: 1.00" in out and "1/1" in out
