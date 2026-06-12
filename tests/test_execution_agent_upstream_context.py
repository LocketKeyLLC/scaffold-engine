"""§17.477 (Phase 3) — confidence-aware upstream context.

`_fetch_upstream_outputs` now returns (text, confidence); `_format_upstream_block`
annotates each upstream section with its verifier confidence and, when over the
size cap, weights the per-node char budget by confidence×length (NULL→0.5).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import execution_agent
from app.modules.execution_agent import _format_upstream_block


@pytest.fixture
def _budget(monkeypatch):
    """Small, predictable budget so truncation fires deterministically."""
    monkeypatch.setattr(execution_agent.settings, "max_upstream_chars", 1000)
    monkeypatch.setattr(execution_agent.settings, "compile_output_min_chunk", 10)


@pytest.mark.smoke
class TestFormatUpstreamBlock:
    def test_empty_returns_empty(self):
        assert _format_upstream_block({}) == ""

    def test_confidence_annotation_present_and_omitted(self):
        block = _format_upstream_block({
            "T1": ("alpha", 0.82),
            "T2": ("beta", None),
        })
        assert "### T1 (confidence: 0.82)" in block      # annotated
        assert "### T2\n" in block                        # None → no suffix
        assert "alpha" in block and "beta" in block
        # The MANDATORY-CONTEXT framing is preserved.
        assert "MANDATORY CONTEXT" in block and "YOUR TASK" in block

    def test_defensive_bare_string(self):
        # A mock that returns a plain string (not a tuple) still renders.
        block = _format_upstream_block({"T1": "just text"})
        assert "### T1\n" in block and "just text" in block

    def test_ranking_favors_high_confidence(self, _budget, monkeypatch):
        monkeypatch.setattr(
            execution_agent.settings, "upstream_confidence_ranking_enabled", True,
        )
        block = _format_upstream_block({
            "T1": ("X" * 1000, 0.9),
            "T2": ("Z" * 1000, 0.1),
        })
        # Same length, but high-confidence T1 keeps substantially more content.
        assert block.count("X") > block.count("Z")

    def test_ranking_disabled_is_proportional(self, _budget, monkeypatch):
        monkeypatch.setattr(
            execution_agent.settings, "upstream_confidence_ranking_enabled", False,
        )
        block = _format_upstream_block({
            "T1": ("X" * 1000, 0.9),
            "T2": ("Z" * 1000, 0.1),
        })
        # Equal lengths → equal budget regardless of confidence (legacy).
        # (±1 char of truncation rounding is not a real asymmetry.)
        assert abs(block.count("X") - block.count("Z")) <= 2

    def test_null_confidence_neutral(self, _budget, monkeypatch):
        monkeypatch.setattr(
            execution_agent.settings, "upstream_confidence_ranking_enabled", True,
        )
        # Two equal-length NULL-confidence nodes → both weighted 0.5 → equal
        # budget (un-verified nodes are neither favored nor starved).
        block = _format_upstream_block({
            "T1": ("X" * 1000, None),
            "T2": ("Z" * 1000, None),
        })
        assert abs(block.count("X") - block.count("Z")) <= 2


@pytest.mark.smoke
async def test_fetch_upstream_outputs_returns_confidence():
    rows = MagicMock()
    rows.fetchall.return_value = [
        SimpleNamespace(node_key="T1", output_text="hello", confidence=0.77),
        SimpleNamespace(node_key="T2", output_text=None, confidence=None),
    ]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=rows)
    result = await execution_agent._fetch_upstream_outputs(db, "job-1", ["T1", "T2"])
    assert result["T1"] == ("hello", 0.77)
    assert result["T2"] == ("", None)      # NULL output → "", NULL conf → None


@pytest.mark.smoke
async def test_fetch_upstream_outputs_empty_deps():
    db = AsyncMock()
    result = await execution_agent._fetch_upstream_outputs(db, "job-1", [])
    assert result == {}
