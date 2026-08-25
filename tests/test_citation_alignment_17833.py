"""§17.833 (plan 8.3 / audit M8) — inline [n] ↔ Sources-block alignment.

M8: the cite-aware summary numbered its inline ``[n]`` markers from
`_build_numbered_summary_sources` (content-required, cap 10) while the
rendered ``**Sources**`` block came from `_build_sources_list` (no content
filter, cap 15, independently sorted) — so ``[3]`` could cite a different
URL than bullet 3. Now the block is rendered FROM the numbered list when
``cite_sources`` is passed; uncitable extras follow unnumbered.
"""
from __future__ import annotations

import pytest

from app.modules.research_agent import (
    _attach_sources_block,
    _build_numbered_summary_sources,
    _build_research_complete_payload,
)
from app.modules.research_state import ResearchState

pytestmark = pytest.mark.smoke


def _state_with_entries() -> ResearchState:
    state = ResearchState(topic="t")
    state.all_entries = [
        # High-confidence entry WITHOUT content — citeable list excludes it,
        # the plain sources list ranks it first: the exact M8 divergence.
        {"source": "http://a.example.com/", "content": "",
         "source_type": "official_docs", "confidence_score": 0.95},
        {"source": "http://b.example.com/", "content": "b content",
         "source_type": "tech_docs", "confidence_score": 0.9},
        {"source": "http://c.example.com/", "content": "c content",
         "source_type": "community", "confidence_score": 0.5},
    ]
    return state


class TestCiteModeBlockAlignment:
    def test_numbered_lines_match_cite_list_order(self):
        state = _state_with_entries()
        cite = _build_numbered_summary_sources(state)
        assert [s["url"] for s in cite] == [
            "http://b.example.com/", "http://c.example.com/",
        ]  # content-required, confidence-ranked

        out = _attach_sources_block("Summary [1][2].", state, cite_sources=cite)
        lines = out.split("\n")
        assert any(l.startswith("1. http://b.example.com/") for l in lines)
        assert any(l.startswith("2. http://c.example.com/") for l in lines)
        # The content-less source appears unnumbered, NOT as a numbered line.
        assert any(l.startswith("- http://a.example.com/") for l in lines)
        assert not any(l.startswith("3.") for l in lines)
        assert "numbered = citeable as [n]" in out

    def test_default_path_byte_unchanged(self):
        state = _state_with_entries()
        legacy = _attach_sources_block("Summary.", state)
        assert "numbered = citeable" not in legacy
        assert "- http://a.example.com/" in legacy  # plain bullets, old shape
        assert _attach_sources_block("Summary.", state, cite_sources=None) == legacy

    def test_payload_carries_cited_sources_when_stamped(self):
        state = _state_with_entries()
        state.cited_sources = [
            {"url": "http://b.example.com/", "source_type": "tech_docs",
             "confidence_score": 0.9},
        ]
        payload = _build_research_complete_payload(
            state, "sid", mode="topic", duration_ms=1,
        )
        assert payload["cited_sources"] == state.cited_sources
        assert payload["sources"]  # full list still present for back-compat

    def test_payload_cited_sources_none_on_default_path(self):
        payload = _build_research_complete_payload(
            ResearchState(topic="t"), "sid", mode="topic", duration_ms=1,
        )
        assert payload["cited_sources"] is None
