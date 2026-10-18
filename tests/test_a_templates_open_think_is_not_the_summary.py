"""§17.1642 — reasoning a thinking model's TEMPLATE opened is not the research summary.

Live (research d4b7ce68 "How to increase the number of working indexers in Prowlarr…", 2026-10-18): the stored
summary began "Let me analyze the task. I need to write a research summary…" and ran about eighty lines of the
model's planning ("Let me map citations", "Let me draft.") to a lone `</think>` before the real summary. The opening
`<think>` was in the model's chat template, not its output, so `strip_think_tags` -- which knew a closed block and an
unclosed opener -- matched neither, and research never called it anyway.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import research_agent as RA
from app.modules.research_state import ResearchState
from app.utils.llm_parsing import strip_think_tags

LIVE = ("Let me analyze the task. I need to write a research summary about increasing working indexers in "
        "Prowlarr…\n\nLet me map citations:\n\n- Prowlarr is an indexer manager [3].\n\nWrite it now.</think>"
        "# Research Summary: Increasing Working Indexers in Prowlarr for Sonarr/Radarr\n\n"
        "## How Prowlarr feeds Sonarr and Radarr\nProwlarr is an indexer manager/proxy [3].")


def test_the_live_text_keeps_only_the_answer():
    out = strip_think_tags(LIVE)
    assert out.startswith("# Research Summary: Increasing Working Indexers")
    assert "Let me" not in out and "</think>" not in out


def test_what_was_already_handled_still_is():
    assert strip_think_tags("hello <think>reason</think> world") == "hello  world"
    assert strip_think_tags("keep this <think>then truncated forever") == "keep this"
    assert strip_think_tags("a plain answer, no tags") == "a plain answer, no tags"
    # an orphan closer AFTER a closed block: the closed block goes, and so does the template-opened prefix
    assert strip_think_tags("plan</think>answer <think>x</think>more") == "answer more"


@pytest.mark.asyncio
async def test_the_summary_is_stored_without_the_reasoning(monkeypatch):
    state = ResearchState(topic="Prowlarr indexers")
    state.all_entries = [{"source": "http://a", "content": "Prowlarr syncs indexers.", "confidence_score": 0.9,
                          "source_type": "web", "facet": "f1"}]
    monkeypatch.setattr(RA.settings, "citation_faithfulness_check_enabled", False)
    monkeypatch.setattr(RA, "_generate_options", AsyncMock(return_value=None))
    with patch.object(RA.model_router, "generate", new=AsyncMock(return_value=SimpleNamespace(success=True, text=LIVE))):
        out = await RA._generate_summary(state)
    assert "Let me analyze" not in out and "</think>" not in out
    assert "Research Summary: Increasing Working Indexers" in out


@pytest.mark.asyncio
async def test_a_reply_that_is_only_reasoning_counts_as_empty(monkeypatch):
    state = ResearchState(topic="t")
    state.all_entries = [{"source": "http://a", "content": "c", "confidence_score": 0.9, "source_type": "web"}]
    monkeypatch.setattr(RA.settings, "citation_faithfulness_check_enabled", False)
    gen = AsyncMock(return_value=SimpleNamespace(success=True, text="only planning, no answer</think>"))
    with patch.object(RA.model_router, "generate", new=gen):
        out = await RA._generate_summary(state)
    assert gen.await_count == 2 and "Full summary unavailable" in out
