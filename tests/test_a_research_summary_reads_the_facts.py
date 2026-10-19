"""§17.1663 — the cited research summary is handed every fact of a source, ranked by how much the source answers.

Live (research ce31a443, 2026-10-19): 756 facts extracted, incl. "NZB Finder … open registration, no invite … $15 per year";
the summary said registration status was undocumented. It got 10 sources ranked by SOURCE TYPE (docs 1.00 > reviews 0.70),
one fact per URL, 500 chars each.
"""
from __future__ import annotations

from app.modules import research_agent as RA
from app.modules.research_state import ResearchState


def _state():
    s = ResearchState(topic="where to sign up for indexers")
    s.all_entries = [{"source": f"https://wiki.example/doc{i}", "content": f"Prowlarr supports indexers, page {i}.",
                      "confidence_score": 1.0, "source_type": "official_docs"} for i in range(12)]
    s.all_entries += [{"source": "https://reviews.example/indexers/", "content": fact, "confidence_score": 0.7,
                       "source_type": "web"} for fact in (
        "NZB Finder has open registration with no invite.", "NZB Finder costs $15 per year.",
        "NZB Geek has open registration.", "NinjaCentral opens registration a few times per year.",
        "DrunkenSlug is invite-only.")]
    return s


def test_a_source_with_many_facts_outranks_one_line_docs_and_keeps_them_all():
    srcs = RA._build_numbered_summary_sources(_state())
    assert srcs[0]["url"] == "https://reviews.example/indexers/"
    for fact in ("open registration with no invite", "$15 per year", "NZB Geek has open", "NinjaCentral", "invite-only"):
        assert fact in srcs[0]["text"], fact
    assert len(srcs) == min(13, RA._CITE_SUMMARY_MAX_SOURCES) and all(len(s["text"]) <= RA._CITE_SUMMARY_SRC_CHARS for s in srcs)


def test_every_numbered_source_fits_the_prompt():
    srcs = RA._build_numbered_summary_sources(_state())
    body = RA._build_cite_summary_prompt_body(srcs)
    assert f"[{len(srcs)}] " in body
