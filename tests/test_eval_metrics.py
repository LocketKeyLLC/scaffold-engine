"""Tests for eval_retrieval.py metric calculations.

Uses importlib.util to load eval_retrieval without triggering __main__.
12 tests: MRR (5), Hit@3 (4), Domain Purity (3).
"""

import importlib.util
import pathlib
import pytest

# ---------------------------------------------------------------------------
# Load eval module without executing main()
# ---------------------------------------------------------------------------
_eval_path = pathlib.Path(__file__).parent / "eval_retrieval.py"

_spec = importlib.util.spec_from_file_location("eval_retrieval", str(_eval_path))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

compute_mrr = _mod.compute_mrr
compute_hit_at_k = _mod.compute_hit_at_k
compute_domain_purity = _mod.compute_domain_purity


# ---------------------------------------------------------------------------
# MRR tests
# ---------------------------------------------------------------------------

class TestMRR:
    def test_perfect_rank(self):
        data = [{"expected_doc_ids": {"a"}, "retrieved_doc_ids": ["a", "b", "c"]}]
        assert compute_mrr(data) == 1.0

    def test_second_rank(self):
        data = [{"expected_doc_ids": {"b"}, "retrieved_doc_ids": ["a", "b", "c"]}]
        assert compute_mrr(data) == 0.5

    def test_miss(self):
        data = [{"expected_doc_ids": {"x"}, "retrieved_doc_ids": ["a", "b", "c"]}]
        assert compute_mrr(data) == 0.0

    def test_empty_input(self):
        assert compute_mrr([]) == 0.0

    def test_average_of_two(self):
        data = [
            {"expected_doc_ids": {"a"}, "retrieved_doc_ids": ["a", "b"]},      # RR=1.0
            {"expected_doc_ids": {"b"}, "retrieved_doc_ids": ["a", "b"]},      # RR=0.5
        ]
        assert compute_mrr(data) == 0.75

    def test_multiple_expected_first_wins(self):
        """MRR uses rank of the *first* relevant doc found."""
        data = [{"expected_doc_ids": {"b", "c"}, "retrieved_doc_ids": ["a", "b", "c"]}]
        assert compute_mrr(data) == 0.5  # "b" at rank 2


# ---------------------------------------------------------------------------
# Hit@3 tests
# ---------------------------------------------------------------------------

class TestHitAt3:
    def test_hit_in_top3(self):
        data = [{"expected_doc_ids": {"c"}, "retrieved_doc_ids": ["a", "b", "c", "d"]}]
        assert compute_hit_at_k(data, k=3) == 1.0

    def test_miss_outside_top3(self):
        data = [{"expected_doc_ids": {"d"}, "retrieved_doc_ids": ["a", "b", "c", "d"]}]
        assert compute_hit_at_k(data, k=3) == 0.0

    def test_empty_input(self):
        assert compute_hit_at_k([], k=3) == 0.0

    def test_partial(self):
        data = [
            {"expected_doc_ids": {"a"}, "retrieved_doc_ids": ["a", "b", "c"]},  # hit
            {"expected_doc_ids": {"z"}, "retrieved_doc_ids": ["a", "b", "c"]},  # miss
        ]
        assert compute_hit_at_k(data, k=3) == 0.5


# ---------------------------------------------------------------------------
# Domain Purity tests
# ---------------------------------------------------------------------------

class TestDomainPurity:
    def test_perfect_purity(self):
        data = [{"expected_domain": "eng", "retrieved_domains": ["eng", "eng", "eng"]}]
        assert compute_domain_purity(data) == 1.0

    def test_mixed_domains(self):
        data = [{"expected_domain": "eng", "retrieved_domains": ["eng", "llm", "eng"]}]
        assert abs(compute_domain_purity(data) - 2.0 / 3.0) < 0.001

    def test_empty_results_count_as_pure(self):
        data = [{"expected_domain": "eng", "retrieved_domains": []}]
        assert compute_domain_purity(data) == 1.0

    def test_no_queries(self):
        assert compute_domain_purity([]) == 0.0

    def test_average_across_queries(self):
        data = [
            {"expected_domain": "eng", "retrieved_domains": ["eng", "eng"]},      # 1.0
            {"expected_domain": "llm", "retrieved_domains": ["llm", "eng"]},      # 0.5
        ]
        assert compute_domain_purity(data) == 0.75
