"""§17.430 — unit tests for the deterministic retrieval metrics.

Offline, pure-function tests against known values. Part of the default
suite + smoke tier. The live regression gate that uses these over the real
pipeline is tests/integration/test_retrieval_eval.py.
"""
import math

import pytest

from app.utils.retrieval_metrics import (
    dcg_at_k,
    hit_at_k,
    ndcg_at_k,
    reciprocal_rank,
)

pytestmark = pytest.mark.smoke


# --- hit_at_k ---

def test_hit_at_k_true_within_window():
    assert hit_at_k([False, True, False], 3) == 1.0
    assert hit_at_k([False, True, False], 1) == 0.0  # relevant is at rank 2


def test_hit_at_k_none_relevant():
    assert hit_at_k([False, False], 2) == 0.0


# --- reciprocal_rank ---

def test_rr_first_position():
    assert reciprocal_rank([True, False]) == 1.0


def test_rr_second_position():
    assert reciprocal_rank([False, True]) == pytest.approx(0.5)


def test_rr_none_relevant_is_zero():
    assert reciprocal_rank([False, False, False]) == 0.0


def test_rr_respects_k_window():
    # relevant only at rank 3, but k=2 cuts it off → 0.0
    assert reciprocal_rank([False, False, True], k=2) == 0.0


# --- dcg / ndcg ---

def test_dcg_single_top():
    # one relevant at rank 1 → 1/log2(2) = 1.0
    assert dcg_at_k([True, False], 2) == pytest.approx(1.0)


def test_ndcg_perfect_ranking_is_one():
    # all relevant pulled to the front → DCG == IDCG
    assert ndcg_at_k([True, True, False, False], 4) == pytest.approx(1.0)


def test_ndcg_reversed_ranking_below_one():
    # two relevant, but at ranks 3 and 4 → worse than ideal (ranks 1,2)
    val = ndcg_at_k([False, False, True, True], 4)
    assert 0.0 < val < 1.0


def test_ndcg_no_relevant_is_zero():
    assert ndcg_at_k([False, False], 2) == 0.0


def test_ndcg_single_relevant_at_top_is_one():
    assert ndcg_at_k([True, False, False], 3) == pytest.approx(1.0)


def test_ndcg_matches_hand_computed():
    # relevant at ranks 1 and 3 (0-indexed 0 and 2), k=3.
    # DCG  = 1/log2(2) + 1/log2(4) = 1.0 + 0.5 = 1.5
    # IDCG = 1/log2(2) + 1/log2(3) = 1.0 + 0.6309 = 1.6309
    rels = [True, False, True]
    expected = (1.0 + 0.5) / (1.0 + 1.0 / math.log2(3))
    assert ndcg_at_k(rels, 3) == pytest.approx(expected, rel=1e-6)
