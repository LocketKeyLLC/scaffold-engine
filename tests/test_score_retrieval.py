"""Unit tests for retrieval scoring math (§17.230 — title-substring shape)."""
from unittest.mock import AsyncMock, patch

import pytest

from scripts.score_retrieval import (
    _title_hit_at_k,
    _title_matches,
    _title_mrr,
    score_query,
)


class TestTitleMatches:
    """AND-of-substrings, case-insensitive."""

    def test_single_substring_present(self):
        assert _title_matches("Kahn's algorithm", ["Kahn"]) is True

    def test_single_substring_absent(self):
        assert _title_matches("DFS topological sort", ["Kahn"]) is False

    def test_all_substrings_present(self):
        assert _title_matches("Kahn's algorithm explained", ["Kahn", "algorithm"]) is True

    def test_one_substring_missing_blocks_match(self):
        # AND semantic: every substring must be present. Critical invariant —
        # OR semantics would surface unrelated entries (e.g. any "algorithm"
        # title would match a Kahn query).
        assert _title_matches("hash algorithm", ["Kahn", "algorithm"]) is False

    def test_case_insensitive(self):
        assert _title_matches("KAHN's ALGORITHM", ["kahn", "algorithm"]) is True
        assert _title_matches("kahn's algorithm", ["KAHN", "ALGORITHM"]) is True

    def test_empty_substr_list_does_not_match(self):
        # Defensive: empty expectation is not a hit; protects against an
        # accidentally-empty expected_titles_contain field counting as a
        # wildcard match against every retrieved title.
        assert _title_matches("anything", []) is False

    def test_empty_title(self):
        assert _title_matches("", ["Kahn"]) is False


class TestTitleHitAtK:
    def test_first_position_hit(self):
        assert _title_hit_at_k(["Kahn"], ["Kahn's algorithm", "other"], 5) is True

    def test_third_position_hit(self):
        assert _title_hit_at_k(["Kahn"], ["x", "y", "Kahn's algorithm"], 5) is True

    def test_no_match_in_any(self):
        assert _title_hit_at_k(["Kahn"], ["DFS", "BFS", "Dijkstra"], 5) is False

    def test_k_truncates(self):
        # Match at rank 6 should NOT count for top-5.
        titles = ["x"] * 5 + ["Kahn's algorithm"]
        assert _title_hit_at_k(["Kahn"], titles, 5) is False
        assert _title_hit_at_k(["Kahn"], titles, 10) is True

    def test_and_semantic_at_k(self):
        # Top-5 includes one title with only "algorithm" and one with only "Kahn".
        # Neither alone satisfies the AND; result is no hit.
        titles = ["hash algorithm", "Kahn's bio", "unrelated", "unrelated", "unrelated"]
        assert _title_hit_at_k(["Kahn", "algorithm"], titles, 5) is False


class TestTitleMRR:
    def test_first_position(self):
        assert _title_mrr(["Kahn"], ["Kahn's algorithm", "other"]) == 1.0

    def test_third_position(self):
        assert _title_mrr(["Kahn"], ["x", "y", "Kahn"]) == pytest.approx(1 / 3)

    def test_not_found(self):
        assert _title_mrr(["Kahn"], ["DFS", "BFS"]) == 0.0

    def test_and_semantic_picks_first_full_match(self):
        # First partial match at rank 1 ("algorithm" only) doesn't satisfy AND;
        # first full match is at rank 2. MRR should reflect rank 2.
        titles = ["hash algorithm", "Kahn's algorithm", "other"]
        assert _title_mrr(["Kahn", "algorithm"], titles) == 0.5


class TestScoreQuery:
    @pytest.mark.asyncio
    async def test_title_hit_at_top(self):
        mock = {"results": [
            {"entry_id": "e1", "title": "Kahn's algorithm explained"},
            {"entry_id": "e2", "title": "other"},
        ]}
        with patch("scripts.score_retrieval.query_rag", AsyncMock(return_value=mock)):
            r = await score_query({
                "query": "Kahn topological sort",
                "expected_titles_contain": ["Kahn", "algorithm"],
                "expected_entry_ids": ["scaffold-kahn-OLDHASH"],
            })
        assert r.title_hit_at_5 is True
        assert r.title_hit_at_10 is True
        assert r.title_mrr == 1.0
        # Exact-id archival metric: old hash drifted, so this is False even
        # though the title-based metric is a hit. This is the §17.229
        # observation made measurable.
        assert r.exact_id_hit is False

    @pytest.mark.asyncio
    async def test_title_miss(self):
        mock = {"results": [{"entry_id": "e1", "title": "unrelated"}]}
        with patch("scripts.score_retrieval.query_rag", AsyncMock(return_value=mock)):
            r = await score_query({
                "query": "q",
                "expected_titles_contain": ["Kahn"],
                "expected_entry_ids": [],
            })
        assert r.title_hit_at_5 is False
        assert r.title_hit_at_10 is False
        assert r.title_mrr == 0.0
        assert r.exact_id_hit is False

    @pytest.mark.asyncio
    async def test_exact_id_hit_archival_still_tracked(self):
        # Pre-§17.230 shape: caller provided expected_entry_ids and the
        # retrieval returned the same id. Title-substring may or may not
        # match; exact-id archival metric should fire independently.
        mock = {"results": [
            {"entry_id": "e1", "title": "completely different title"},
        ]}
        with patch("scripts.score_retrieval.query_rag", AsyncMock(return_value=mock)):
            r = await score_query({
                "query": "q",
                "expected_titles_contain": ["Kahn", "algorithm"],
                "expected_entry_ids": ["e1"],
            })
        assert r.exact_id_hit is True
        assert r.title_hit_at_5 is False  # AND of Kahn+algorithm not in title

    @pytest.mark.asyncio
    async def test_context_precision_recall_populated(self):
        # §17.794 — two label-matching entries at ranks 1 and 3 of 4 retrieved.
        # rels = [T, F, T, F]; context precision = (1/1 + 2/3)/2 = 0.8333.
        # n_relevant defaults to len(expected_entry_ids)=1 → recall caps at 1.0.
        mock = {"results": [
            {"entry_id": "e1", "title": "Kahn's algorithm A"},
            {"entry_id": "e2", "title": "unrelated"},
            {"entry_id": "e3", "title": "Kahn's algorithm B"},
            {"entry_id": "e4", "title": "unrelated"},
        ]}
        with patch("scripts.score_retrieval.query_rag", AsyncMock(return_value=mock)):
            r = await score_query({
                "query": "q",
                "expected_titles_contain": ["Kahn", "algorithm"],
                "expected_entry_ids": ["x"],
            })
        assert r.context_precision == pytest.approx((1.0 + 2.0 / 3.0) / 2.0)
        assert r.context_recall == pytest.approx(1.0)
        assert r.n_relevant == 1
        assert r.faithfulness is None  # not requested → unscored

    @pytest.mark.asyncio
    async def test_context_recall_multi_target_denominator(self):
        # §17.794 — 3 labelled targets (expected_entry_ids), one label-matching
        # doc retrieved → recall = 1/3.
        mock = {"results": [
            {"entry_id": "e1", "title": "Bitnami Milvus chart"},
            {"entry_id": "e2", "title": "unrelated"},
        ]}
        with patch("scripts.score_retrieval.query_rag", AsyncMock(return_value=mock)):
            r = await score_query({
                "query": "q",
                "expected_titles_contain": ["bitnami", "milvus"],
                "expected_entry_ids": ["a", "b", "c"],
            })
        assert r.n_relevant == 3
        assert r.context_recall == pytest.approx(1.0 / 3.0)
