"""
tests/test_rag_pipeline.py — RAG pipeline smoke tests

Source-analysis tests that verify module structure, function signatures,
and integration points without loading heavy dependencies.
"""

import os
import re
import ast
import pytest

_ABS_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "app", "modules", "rag_pipeline.py"
))

pytestmark = pytest.mark.skipif(
    not os.path.exists(_ABS_PATH),
    reason="rag_pipeline.py not found",
)


def _source():
    with open(_ABS_PATH) as f:
        return f.read()


def _ast_tree():
    return ast.parse(_source())


def _find_async_func(name: str):
    """Find an async function def by name in the AST."""
    for node in ast.walk(_ast_tree()):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def _find_func(name: str):
    """Find any function def by name in the AST."""
    for node in ast.walk(_ast_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


# ===========================================================================
# Module structure
# ===========================================================================

class TestModuleStructure:
    def test_query_rag_exists(self):
        fn = _find_func("query_rag")
        assert fn is not None, "query_rag function should exist"

    def test_query_rag_is_async(self):
        fn = _find_async_func("query_rag")
        assert fn is not None, "query_rag should be async"

    def test_module_references_milvus(self):
        src = _source()
        assert any(t in src for t in ["milvus", "Milvus", "Collection"]), \
            "Module should reference Milvus"

    def test_module_references_embeddings(self):
        src = _source()
        assert any(t in src for t in ["embed", "embedding", "4096"]), \
            "Module should reference embedding model"


# ===========================================================================
# query_rag signature
# ===========================================================================

class TestQueryRagSignature:
    def test_accepts_domain_param(self):
        fn = _find_func("query_rag")
        params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        assert "domain" in params, f"query_rag should accept domain. Got: {params}"

    def test_accepts_top_k_param(self):
        fn = _find_func("query_rag")
        params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        assert "top_k" in params, f"query_rag should accept top_k. Got: {params}"

    def test_accepts_query_param(self):
        fn = _find_func("query_rag")
        params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        assert any(p in params for p in ["query", "query_text", "text"]), \
            f"query_rag should accept a query param. Got: {params}"


# ===========================================================================
# Domain filtering
# ===========================================================================

class TestDomainFiltering:
    def test_domain_filter_in_source(self):
        src = _source()
        assert "domain" in src, "Module should reference domain filtering"

    def test_valid_domains_referenced(self):
        src = _source()
        domain_hits = sum(1 for d in ["prompt", "rag", "eng", "llm", "spec"]
                         if d in src)
        assert domain_hits >= 1 or "VALID_DOMAINS" in src, \
            "Module should reference valid domain values"

    def test_expr_filter_for_domain(self):
        src = _source()
        assert any(t in src for t in ["expr", "filter", "domain =="]), \
            "Module should build Milvus filter expression for domain"


# ===========================================================================
# Reranker integration
# ===========================================================================

class TestRerankerIntegration:
    def test_imports_reranker(self):
        src = _source()
        assert any(t in src for t in ["rerank", "cross_encoder", "CrossEncoder"]), \
            "Module should import reranker"

    def test_rrf_fusion(self):
        src = _source()
        assert any(t in src for t in ["rrf", "RRF", "reciprocal", "fusion"]), \
            "Module should implement RRF fusion"


# ===========================================================================
# Score structure
# ===========================================================================

class TestScoreStructure:
    def test_multi_score_fields(self):
        src = _source()
        score_hits = sum(1 for t in ["vector", "rrf", "rerank", "scores"] if t in src)
        assert score_hits >= 2, "Module should produce multi-score results"

    def test_dataclass_for_results(self):
        src = _source()
        assert "@dataclass" in src, "Module should use dataclass for result types"
