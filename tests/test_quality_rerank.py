"""Tests for §17.120 — quality_signal-weighted rerank."""
from __future__ import annotations

import pytest

from app.modules.quality_rerank import quality_bump


@pytest.mark.smoke
class TestQualityBumpNoSignal:
    def test_none_signal_returns_one(self):
        assert quality_bump("so_answer", None) == 1.0

    def test_empty_signal_returns_one(self):
        assert quality_bump("so_answer", {}) == 1.0

    def test_unknown_source_type_returns_one(self):
        assert quality_bump("not_a_source", {"score": 1000, "is_accepted": True}) == 1.0


@pytest.mark.smoke
class TestQualityBumpSO:
    def test_accepted_only(self):
        # No score, just accepted
        assert quality_bump("so_answer", {"is_accepted": True, "score": 0}) == pytest.approx(1.10)

    def test_accepted_plus_medium_score(self):
        # accepted +0.10 plus score>=50 +0.05 = 1.15
        assert quality_bump("so_answer", {"is_accepted": True, "score": 100}) == pytest.approx(1.15)

    def test_accepted_plus_high_score(self):
        # accepted +0.10 plus score>=200 +0.10 = 1.20 (at cap)
        assert quality_bump("so_answer", {"is_accepted": True, "score": 500}) == pytest.approx(1.20)

    def test_unaccepted_high_score(self):
        # Not accepted; score>=200 → +0.10
        assert quality_bump("so_answer", {"is_accepted": False, "score": 250}) == pytest.approx(1.10)

    def test_low_score_unaccepted(self):
        assert quality_bump("so_answer", {"is_accepted": False, "score": 5}) == pytest.approx(1.0)


@pytest.mark.smoke
class TestQualityBumpHN:
    @pytest.mark.parametrize("points,expected", [
        (0, 1.0), (50, 1.0), (100, 1.05), (250, 1.05), (500, 1.10), (5000, 1.10),
    ])
    def test_tiers(self, points, expected):
        assert quality_bump("hn_comment", {"points": points}) == pytest.approx(expected)


@pytest.mark.smoke
class TestQualityBumpReddit:
    @pytest.mark.parametrize("score,expected", [
        (0, 1.0), (50, 1.0), (100, 1.05), (300, 1.05), (500, 1.10), (5000, 1.10),
    ])
    def test_tiers(self, score, expected):
        assert quality_bump("reddit_post", {"score": score}) == pytest.approx(expected)


@pytest.mark.smoke
class TestQualityBumpCommunity:
    @pytest.mark.parametrize("reactions,expected", [
        (0, 1.0), (3, 1.0), (5, 1.05), (10, 1.05), (20, 1.10), (100, 1.10),
    ])
    def test_tiers(self, reactions, expected):
        assert quality_bump("community", {"positive_reactions": reactions}) == pytest.approx(expected)


@pytest.mark.smoke
class TestQualityBumpHFCards:
    @pytest.mark.parametrize("source_type", ["model_card", "dataset_card"])
    @pytest.mark.parametrize("likes,expected", [
        (0, 1.0), (50, 1.0), (100, 1.05), (500, 1.05), (1000, 1.10), (50000, 1.10),
    ])
    def test_tiers(self, source_type, likes, expected):
        assert quality_bump(source_type, {"likes": likes}) == pytest.approx(expected)


@pytest.mark.smoke
class TestQualityBumpPaper:
    def test_low_upvotes_no_bump(self):
        assert quality_bump("paper_abstract", {"upvotes": 10}) == pytest.approx(1.0)

    def test_high_upvotes_bump(self):
        assert quality_bump("paper_abstract", {"upvotes": 100}) == pytest.approx(1.05)


@pytest.mark.smoke
class TestQualityBumpCap:
    def test_caps_at_1_20(self):
        # SO: accepted (+0.10) + score>=200 (+0.10) already = 1.20
        # Add hypothetical extra +0.10 — should still cap at 1.20
        assert quality_bump("so_answer", {"is_accepted": True, "score": 9999}) == pytest.approx(1.20)

    def test_neutral_source_types_no_bump(self):
        # These have provenance but no scoring signal worth weighting on
        for stype in ("release_notes", "test_code", "ci_config",
                      "wiki_article", "tech_docs"):
            assert quality_bump(stype, {"any": "value"}) == pytest.approx(1.0)
