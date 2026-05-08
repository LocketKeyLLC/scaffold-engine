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
    """Build a mock model_router.generate / tool_call response.

    Sprint W.6: research_agent migrated from generate to tool_call. The
    helper now also pre-populates ``tool_calls`` based on the text shape
    so existing fixtures (JSON object/array strings) work for both code
    paths without per-test edits:

      - text parses as a JSON object → tool_calls=[ToolCall(args=parsed)]
      - text parses as a JSON array  → tool_calls=[ToolCall(args={"entries": parsed})]
      - else (prose / plaintext)     → tool_calls=[]
    """
    from app.providers.base import ToolCall
    resp = types.SimpleNamespace()
    resp.success = success
    resp.text = text
    resp.error = None if success else "mock error"
    resp.tool_calls = []
    if success and text:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            resp.tool_calls = [ToolCall(id="t0", name="mock_tool", arguments=parsed)]
        elif isinstance(parsed, list):
            resp.tool_calls = [ToolCall(
                id="t0", name="mock_tool",
                arguments={"entries": parsed},
            )]
    return resp


def _make_tool_call_response(arguments: dict, success: bool = True, name: str = "mock_tool"):
    """Build a mock model_router.tool_call response with explicit args."""
    from app.providers.base import ToolCall
    resp = types.SimpleNamespace()
    resp.success = success
    resp.text = ""
    resp.error = None if success else "mock error"
    resp.tool_calls = [ToolCall(id="t0", name=name, arguments=arguments)] if success else []
    return resp


def _wire_router(mock_mr, response):
    """Bind both generate and tool_call on a patched model_router mock to the
    same response. Sprint W.6 — research_agent migrated from generate to
    tool_call but several tests still patch mock_mr.generate. Wiring both
    ensures whichever call path the production code takes is intercepted.
    The shared response shape (.text + .tool_calls + .success) lets one
    fixture serve both APIs."""
    mock_mr.generate = AsyncMock(return_value=response)
    mock_mr.tool_call = AsyncMock(return_value=response)
    return mock_mr

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


__all__ = ['AsyncMock', 'GOOD_DECOMPOSITION', 'GOOD_EXTRACTION', 'GOOD_GAP_ANALYSIS', 'MOCK_SEARCH_RESULTS', 'MagicMock', '__all__', '_check_contradictions', '_make_generate_response', '_make_tool_call_response', 'asyncio', 'json', 'patch', 'pytest', 'sql_text', 'types', 'uuid']
