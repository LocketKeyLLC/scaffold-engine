"""Unit tests for retrieval scoring math."""
from unittest.mock import AsyncMock, patch

import pytest

from scripts.score_retrieval import _mrr, _recall_at_k, score_query


class TestRecallAtK:
    def test_full_recall(self):
        assert _recall_at_k({"a", "b"}, ["a", "b", "c"], 5) == 1.0

    def test_partial_recall(self):
        assert _recall_at_k({"a", "b"}, ["a", "x", "y"], 5) == 0.5

    def test_zero_recall(self):
        assert _recall_at_k({"a"}, ["x", "y"], 5) == 0.0

    def test_empty_expected(self):
        assert _recall_at_k(set(), ["a"], 5) == 0.0

    def test_k_truncates(self):
        assert _recall_at_k({"a"}, ["x"] * 5 + ["a"], 5) == 0.0


class TestMRR:
    def test_first_position(self):
        assert _mrr({"a"}, ["a", "b", "c"]) == 1.0

    def test_third_position(self):
        assert _mrr({"a"}, ["x", "y", "a"]) == pytest.approx(1 / 3)

    def test_not_found(self):
        assert _mrr({"a"}, ["x", "y"]) == 0.0


class TestScoreQuery:
    @pytest.mark.asyncio
    async def test_hit_computes_metrics(self):
        mock_result = {"results": [{"entry_id": "e1"}, {"entry_id": "e2"}]}
        with patch("scripts.score_retrieval.query_rag", AsyncMock(return_value=mock_result)):
            r = await score_query({"query": "q", "expected_entry_ids": ["e1"]})
        assert r.hit is True
        assert r.mrr == 1.0
        assert r.recall_at_5 == 1.0

    @pytest.mark.asyncio
    async def test_miss(self):
        mock_result = {"results": [{"entry_id": "x"}]}
        with patch("scripts.score_retrieval.query_rag", AsyncMock(return_value=mock_result)):
            r = await score_query({"query": "q", "expected_entry_ids": ["e1"]})
        assert r.hit is False
        assert r.mrr == 0.0
