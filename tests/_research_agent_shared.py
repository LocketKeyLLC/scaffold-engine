"""Shared fixtures and helpers for test_research_agent_*.py files (#9.6).

All module-level imports and helper functions from the original
test_research_agent.py live here, so split files can `from ... import *`.

Leading underscore in the filename -> pytest skips collection.
"""
"""
Behavioral tests for app.modules.research_agent.

Follows project patterns from test_rag_pipeline.py and test_ideation_workflow.py:
mock all external deps (Ollama, SearXNG, Milvus), call real functions, assert outputs.

Run in-container:
    python -m pytest tests/test_research_agent.py -v
"""

import asyncio

import json

import types

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

def _make_generate_response(text: str, success: bool = True):
    """Build a mock model_router.generate response."""
    resp = types.SimpleNamespace()
    resp.success = success
    resp.text = text
    resp.error = None if success else "mock error"
    return resp

GOOD_DECOMPOSITION = json.dumps({
    "topic_complexity": "medium",
    "facets": ["overview", "performance", "security"],
    "queries": [
        {"query": "Redis caching overview", "facet": "overview", "priority": "high", "search_category": "general"},
        {"query": "Redis performance tuning", "facet": "performance", "priority": "medium", "search_category": "it"},
        {"query": "Redis security best practices", "facet": "security", "priority": "medium", "search_category": "general"},
    ],
})

GOOD_EXTRACTION = json.dumps([
    {
        "title": "Redis default port",
        "content": "Redis listens on port 6379 by default.",
        "tags": "redis,networking",
        "source": "https://redis.io/docs",
        "confidence_score": 0.95,
        "source_type": "official_docs",
        "facet": "overview",
    },
    {
        "title": "Redis pipelining",
        "content": "Pipelining reduces round-trip latency by batching commands.",
        "tags": "redis,performance",
        "source": "https://redis.io/docs/pipelining",
        "confidence_score": 0.90,
        "source_type": "official_docs",
        "facet": "performance",
    },
])

GOOD_GAP_ANALYSIS = json.dumps({
    "coverage_pct": 60,
    "covered_facets": ["overview"],
    "gap_facets": ["security"],
    "gap_queries": [
        {"query": "Redis ACL authentication", "facet": "security", "priority": "high", "search_category": "general"},
    ],
    "assessment": "Overview well covered. Security facet needs more research.",
})

MOCK_SEARCH_RESULTS = [
    {"title": "Redis Intro", "url": "https://redis.io/intro", "content": "Redis is an in-memory store."},
    {"title": "Redis Perf", "url": "https://redis.io/perf", "content": "Redis can handle 100k ops/sec."},
]

import pytest

from app.modules.research_agent import _check_contradictions

import uuid

from sqlalchemy import text as sql_text


__all__ = ['AsyncMock', 'GOOD_DECOMPOSITION', 'GOOD_EXTRACTION', 'GOOD_GAP_ANALYSIS', 'MOCK_SEARCH_RESULTS', 'MagicMock', '__all__', '_check_contradictions', '_make_generate_response', 'asyncio', 'json', 'patch', 'pytest', 'sql_text', 'types', 'uuid']
