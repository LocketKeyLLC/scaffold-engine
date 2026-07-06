"""§17.558 — unit tests for the research-quality coverage scorer.

Pure logic only (no model calls); grounding is LLM-judged and exercised live
by scripts/score_research.py, not here.
"""
from __future__ import annotations

from scripts.score_research import _facet_hit, facet_coverage


_FACETS = [
    {"name": "segmentation", "contains": ["segment"]},
    {"name": "dos-interrupts", "contains": ["21h"]},
    {"name": "protected-mode", "contains": ["protected mode"]},
]


def test_facet_hit_and_substring_case_insensitive():
    assert _facet_hit("The SEGMENT register holds...", ["segment"]) is True
    # AND semantics: all substrings must be present
    assert _facet_hit("segment only", ["segment", "offset"]) is False
    assert _facet_hit("segment and offset", ["segment", "offset"]) is True


def test_facet_hit_empty_contains_is_false():
    assert _facet_hit("anything", []) is False


def test_coverage_all_facets_present():
    summary = "It uses segment:offset addressing, INT 21h services, and protected mode."
    cov = facet_coverage(summary, _FACETS)
    assert cov["covered"] == 3 and cov["total"] == 3
    assert cov["coverage"] == 1.0 and cov["missed"] == []


def test_coverage_partial_reports_missed():
    summary = "It uses segment:offset addressing and INT 21h services."  # no protected mode
    cov = facet_coverage(summary, _FACETS)
    assert cov["covered"] == 2 and cov["coverage"] == 2 / 3
    assert cov["missed"] == ["protected-mode"]


def test_coverage_empty_facets_is_zero_not_crash():
    cov = facet_coverage("anything", [])
    assert cov == {"covered": 0, "total": 0, "coverage": 0.0, "missed": []}
