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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ============================================================================
# TestDecomposeTopic
# ============================================================================

class TestDecomposeTopic:
    """Tests for _decompose_topic() - LLM decomposes topic into queries."""

    @pytest.mark.asyncio
    async def test_parses_valid_json(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))
            from app.modules.research_agent import _decompose_topic

            result = await _decompose_topic("Redis caching", model="qwen3:4b")

            assert "queries" in result
            assert "facets" in result
            assert len(result["queries"]) == 3
            assert result["topic_complexity"] == "medium"

    @pytest.mark.asyncio
    async def test_fallback_on_bad_json(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response("not json at all"))
            from app.modules.research_agent import _decompose_topic

            result = await _decompose_topic("Redis caching", model="qwen3:4b")

            assert "queries" in result
            assert len(result["queries"]) >= 3
            assert result["facets"] == ["Redis caching"]

    @pytest.mark.asyncio
    async def test_fallback_on_llm_failure(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response("", success=False))
            from app.modules.research_agent import _decompose_topic

            result = await _decompose_topic("Redis caching", model="qwen3:4b")

            assert "queries" in result
            assert result["topic_complexity"] == "medium"

    @pytest.mark.asyncio
    async def test_existing_facets_in_prompt(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))
            from app.modules.research_agent import _decompose_topic

            await _decompose_topic(
                "Redis caching", model="qwen3:4b",
                existing_facets=["overview", "performance"],
                gap_focus="security aspects",
            )

            prompt_text = mock_mr.generate.call_args[0][0]
            assert "overview" in prompt_text
            assert "security aspects" in prompt_text


# ============================================================================
# TestSearchQueries
# ============================================================================

class TestSearchQueries:
    """Tests for _search_queries() - SearXNG search with URL dedup."""

    @pytest.mark.asyncio
    async def test_returns_results(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": [
            {"title": "Result 1", "url": "https://example.com/1", "content": "Content 1"},
            {"title": "Result 2", "url": "https://example.com/2", "content": "Content 2"},
        ]}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        queries = [{"query": "test query", "facet": "test", "search_category": "general"}]

        with patch("app.utils.http_clients.get_searxng_client", return_value=mock_client), \
             patch("app.modules.research_agent.settings") as mock_settings, \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock):
            mock_settings.research_max_queries = 10
            mock_settings.research_searxng_delay = 0
            mock_settings.research_max_urls_per_iteration = 20
            from app.modules.research_agent import _search_queries, ResearchState

            state = ResearchState(topic="test")
            results = await _search_queries(queries, state)

            assert len(results) == 2
            assert results[0]["title"] == "Result 1"
            assert results[0]["url"] == "https://example.com/1"

    @pytest.mark.asyncio
    async def test_skips_duplicate_urls(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": [
            {"title": "Dup", "url": "https://already-seen.com", "content": "old"},
            {"title": "New", "url": "https://new-site.com", "content": "fresh"},
        ]}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        queries = [{"query": "test", "facet": "test", "search_category": "general"}]

        with patch("app.utils.http_clients.get_searxng_client", return_value=mock_client), \
             patch("app.modules.research_agent.settings") as mock_settings, \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock):
            mock_settings.research_max_queries = 10
            mock_settings.research_searxng_delay = 0
            mock_settings.research_max_urls_per_iteration = 20
            from app.modules.research_agent import _search_queries, ResearchState

            state = ResearchState(topic="test")
            state.url_history.add("https://already-seen.com")
            results = await _search_queries(queries, state)

            assert len(results) == 1
            assert results[0]["url"] == "https://new-site.com"

    @pytest.mark.asyncio
    async def test_skips_duplicate_queries(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock()

        queries = [{"query": "already searched", "facet": "test", "search_category": "general"}]

        with patch("app.utils.http_clients.get_searxng_client", return_value=mock_client), \
             patch("app.modules.research_agent.settings") as mock_settings, \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock):
            mock_settings.research_max_queries = 10
            mock_settings.research_searxng_delay = 0
            mock_settings.research_max_urls_per_iteration = 20
            from app.modules.research_agent import _search_queries, ResearchState

            state = ResearchState(topic="test")
            state.search_history.add("already searched")
            results = await _search_queries(queries, state)

            assert len(results) == 0
            mock_client.get.assert_not_awaited()


# ============================================================================
# TestExtractEntries
# ============================================================================

class TestExtractEntries:
    """Tests for _extract_entries() - LLM distills results into entries."""

    @pytest.mark.asyncio
    async def test_extracts_entries(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_EXTRACTION))
            from app.modules.research_agent import _extract_entries

            entries = await _extract_entries(MOCK_SEARCH_RESULTS, "Redis caching", model="qwen2.5:7b")

            assert len(entries) == 2
            assert entries[0]["title"] == "Redis default port"
            assert "confidence_score" in entries[0]

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock()
            from app.modules.research_agent import _extract_entries

            entries = await _extract_entries([], "Redis", model="qwen2.5:7b")

            assert entries == []
            mock_mr.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response("", success=False))
            from app.modules.research_agent import _extract_entries

            entries = await _extract_entries(MOCK_SEARCH_RESULTS, "Redis", model="qwen2.5:7b")

            assert entries == []

    @pytest.mark.asyncio
    async def test_batches_large_input(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_EXTRACTION))
            from app.modules.research_agent import _extract_entries

            results = [
                {"title": f"R{i}", "url": f"https://ex.com/{i}", "content": f"Content {i}"}
                for i in range(15)
            ]
            entries = await _extract_entries(results, "Redis", model="qwen2.5:7b")

            assert mock_mr.generate.await_count == 2
            assert len(entries) == 4


# ============================================================================
# TestAnalyzeGaps
# ============================================================================

class TestAnalyzeGaps:
    """Tests for _analyze_gaps() - coverage gap analysis."""

    @pytest.mark.asyncio
    async def test_parses_gap_response(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_GAP_ANALYSIS))
            from app.modules.research_agent import _analyze_gaps, ResearchState

            state = ResearchState(topic="Redis")
            state.outline_facets = ["overview", "performance", "security"]
            state.all_entries = [{"facet": "overview", "title": "T", "content": "C"}]

            result = await _analyze_gaps(state, model="qwen3:4b")

            assert result["coverage_pct"] == 60
            assert "security" in result["gap_facets"]
            assert len(result["gap_queries"]) == 1

    @pytest.mark.asyncio
    async def test_fallback_on_failure(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response("garbage", success=True))
            from app.modules.research_agent import _analyze_gaps, ResearchState

            state = ResearchState(topic="Redis")
            state.outline_facets = ["overview"]
            state.all_entries = []

            result = await _analyze_gaps(state, model="qwen3:4b")

            assert result["coverage_pct"] == 100
            assert result["gap_queries"] == []


# ============================================================================
# TestRunResearch
# ============================================================================

class TestRunResearch:
    """Tests for run_research() - full async generator yielding SSE events."""

    def _parse_events(self, sse_strings):
        events = []
        for s in sse_strings:
            if not s.strip():
                continue
            lines = s.strip().split("\n")
            event_type = None
            data = None
            for line in lines:
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
            if event_type:
                events.append({"type": event_type, "data": data})
        return events

    @pytest.mark.asyncio
    async def test_emits_research_started_first(self):
        with patch("app.modules.research_agent.model_router") as mock_mr, \
             patch("app.modules.research_agent._search_queries", new_callable=AsyncMock, return_value=[]), \
             patch("app.modules.research_agent._generate_summary", new_callable=AsyncMock, return_value="Done."), \
             patch("app.modules.research_agent.get_model", return_value="qwen3:4b"), \
             patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="test-session-id"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="test-session-id"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.create_task") as mock_task:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))

            done_future = asyncio.Future()
            done_future.set_result("Summary text.")
            mock_task.return_value = done_future

            from app.modules.research_agent import run_research

            events_raw = []
            async for sse in run_research("Redis caching", depth="shallow"):
                events_raw.append(sse)

            events = self._parse_events(events_raw)
            assert events[0]["type"] == "research_started"
            assert events[0]["data"]["topic"] == "Redis caching"
            assert events[0]["data"]["depth"] == "shallow"

    @pytest.mark.asyncio
    async def test_emits_research_complete_last(self):
        with patch("app.modules.research_agent.model_router") as mock_mr, \
             patch("app.modules.research_agent._search_queries", new_callable=AsyncMock, return_value=[]), \
             patch("app.modules.research_agent._generate_summary", new_callable=AsyncMock, return_value="Done."), \
             patch("app.modules.research_agent.get_model", return_value="qwen3:4b"), \
             patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="test-session-id"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="test-session-id"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.create_task") as mock_task:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))

            done_future = asyncio.Future()
            done_future.set_result("Summary text.")
            mock_task.return_value = done_future

            from app.modules.research_agent import run_research

            events_raw = []
            async for sse in run_research("Redis", depth="shallow"):
                events_raw.append(sse)

            events = self._parse_events(events_raw)
            non_heartbeat = [e for e in events if e["type"] != "heartbeat"]
            assert non_heartbeat[-1]["type"] == "research_complete"
            assert "total_ingested" in non_heartbeat[-1]["data"]
            assert "summary" in non_heartbeat[-1]["data"]

    @pytest.mark.asyncio
    async def test_shallow_depth_one_iteration(self):
        with patch("app.modules.research_agent.model_router") as mock_mr, \
             patch("app.modules.research_agent._search_queries", new_callable=AsyncMock, return_value=MOCK_SEARCH_RESULTS), \
             patch("app.modules.research_agent.ingest_entries", new_callable=AsyncMock, return_value={"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 0}), \
             patch("app.modules.research_agent._generate_summary", new_callable=AsyncMock, return_value="Done."), \
             patch("app.modules.research_agent.get_model", return_value="qwen3:4b"), \
             patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="test-session-id"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="test-session-id"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.create_task") as mock_task:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))

            call_count = [0]
            def side_effect(coro):
                f = asyncio.Future()
                if call_count[0] == 0:
                    f.set_result([{"title": "T", "content": "C"}])
                else:
                    f.set_result("Summary.")
                call_count[0] += 1
                coro.close()
                return f
            mock_task.side_effect = side_effect

            from app.modules.research_agent import run_research

            events_raw = []
            async for sse in run_research("Redis", depth="shallow"):
                events_raw.append(sse)

            events = self._parse_events(events_raw)
            iteration_events = [e for e in events if e["type"] == "iteration_started"]
            assert len(iteration_events) == 1

    @pytest.mark.asyncio
    async def test_no_results_breaks_early(self):
        with patch("app.modules.research_agent.model_router") as mock_mr, \
             patch("app.modules.research_agent._search_queries", new_callable=AsyncMock, return_value=[]), \
             patch("app.modules.research_agent._generate_summary", new_callable=AsyncMock, return_value="No data."), \
             patch("app.modules.research_agent.get_model", return_value="qwen3:4b"), \
             patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="test-session-id"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="test-session-id"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.create_task") as mock_task:
            mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))

            done_future = asyncio.Future()
            done_future.set_result("No data.")
            mock_task.return_value = done_future

            from app.modules.research_agent import run_research

            events_raw = []
            async for sse in run_research("Redis", depth="medium"):
                events_raw.append(sse)

            events = self._parse_events(events_raw)
            iter_complete = [e for e in events if e["type"] == "iteration_complete"]
            assert len(iter_complete) == 1
            assert iter_complete[0]["data"]["reason"] == "no_results"


# --- Contradiction detection (#3.4) ---

import pytest
from app.modules.research_agent import _check_contradictions


def test_check_contradictions_flags_shared_words():
    entries = [
        {"title": "Python is a compiled language"},
        {"title": "Python is an interpreted language"},
    ]
    result = _check_contradictions(entries)
    assert len(result) == 1
    assert result[0]["entry_a"] == entries[0]["title"]
    assert result[0]["entry_b"] == entries[1]["title"]
    assert set(result[0]["shared_concepts"]) >= {"python", "is", "language"}


def test_check_contradictions_skips_low_overlap():
    entries = [
        {"title": "Python memory management"},
        {"title": "Rust ownership model"},
    ]
    assert _check_contradictions(entries) == []


def test_check_contradictions_caps_at_five():
    # 6 entries all sharing "shared words here" → C(6,2)=15 candidate pairs
    entries = [{"title": f"shared words here {i}"} for i in range(6)]
    result = _check_contradictions(entries)
    assert len(result) == 5


# ---------------------------------------------------------------------------
# Ingestion breakdown (new / versioned / rejected / skipped_hash)
# ---------------------------------------------------------------------------

class TestIngestionBreakdown:
    """Verify /research surfaces the three-bucket ingest classification."""

    @pytest.mark.asyncio
    async def test_research_complete_contains_breakdown_fields(self):
        """Final research_complete SSE includes new, versioned, rejected, skipped_hash."""
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import run_research

        fake_entries = [{"content": "fact", "facet": "x", "source": "http://a"}]
        fake_stats = {"new": 3, "versioned": 2, "rejected": 1, "skipped_hash": 4}

        with patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="sess-1"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._decompose_topic", new_callable=AsyncMock,
                   return_value={"facets": ["x"], "queries": ["q"], "topic_complexity": "simple"}), \
             patch("app.modules.research_agent._search_queries", new_callable=AsyncMock,
                   return_value=[{"url": "http://a", "title": "t", "content": "c"}]), \
             patch("app.modules.research_agent._extract_entries", new_callable=AsyncMock,
                   return_value=fake_entries), \
             patch("app.modules.research_agent._analyze_gaps", new_callable=AsyncMock,
                   return_value={"coverage_pct": 100, "gap_queries": []}), \
             patch("app.modules.research_agent._generate_summary", new_callable=AsyncMock,
                   return_value="summary"), \
             patch("app.modules.research_agent.ingest_entries", new_callable=AsyncMock,
                   return_value=fake_stats):

            events = []
            async for sse in run_research("test topic", depth="shallow"):
                events.append(sse)

        complete_events = [e for e in events if "research_complete" in e]
        assert len(complete_events) == 1, f"expected 1 research_complete, got {len(complete_events)}"
        payload = complete_events[0]
        assert '"new": 3' in payload, f"missing new=3 in payload: {payload[:400]}"
        assert '"versioned": 2' in payload
        assert '"rejected": 1' in payload
        assert '"skipped_hash": 4' in payload

    @pytest.mark.asyncio
    async def test_breakdown_totals_accumulate_across_iterations(self):
        """Multiple ingest calls sum into the final totals."""
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import run_research

        # Two iterations: first returns {new:2, versioned:1, rejected:0, skipped_hash:0},
        # second returns {new:1, versioned:0, rejected:3, skipped_hash:2}
        call_returns = [
            {"new": 2, "versioned": 1, "rejected": 0, "skipped_hash": 0},
            {"new": 1, "versioned": 0, "rejected": 3, "skipped_hash": 2},
        ]
        ingest_mock = AsyncMock(side_effect=call_returns)

        # Two iterations: first analyze_gaps returns gap queries, second returns empty (converges)
        gap_returns = [
            {"coverage_pct": 50, "gap_queries": ["next-q"]},
            {"coverage_pct": 100, "gap_queries": []},
        ]

        with patch("app.modules.research_agent._guard_concurrent", new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session", new_callable=AsyncMock, return_value="sess-2"), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._decompose_topic", new_callable=AsyncMock,
                   return_value={"facets": ["x"], "queries": ["q"], "topic_complexity": "medium"}), \
             patch("app.modules.research_agent._search_queries", new_callable=AsyncMock,
                   return_value=[{"url": "http://a", "title": "t", "content": "c"}]), \
             patch("app.modules.research_agent._extract_entries", new_callable=AsyncMock,
                   return_value=[{"content": "fact", "facet": "x", "source": "http://a"}]), \
             patch("app.modules.research_agent._analyze_gaps", new_callable=AsyncMock,
                   side_effect=gap_returns), \
             patch("app.modules.research_agent._generate_summary", new_callable=AsyncMock,
                   return_value="summary"), \
             patch("app.modules.research_agent.ingest_entries", ingest_mock):

            events = []
            async for sse in run_research("test topic", depth="medium"):
                events.append(sse)

        payload = [e for e in events if "research_complete" in e][0]
        assert '"new": 3' in payload, payload[:500]       # 2 + 1
        assert '"versioned": 1' in payload                # 1 + 0
        assert '"rejected": 3' in payload                 # 0 + 3
        assert '"skipped_hash": 2' in payload             # 0 + 2



# ============================================================================
# NEW TESTS — critical fixes (#3–#6) + helper extractions (#50, #51, #52)
# Added in Phase D of the research_agent refactor.
# ============================================================================

import uuid
from sqlalchemy import text as sql_text


# ----------------------------------------------------------------------------
# #3 — Atomic claim for resume (real Postgres)
# ----------------------------------------------------------------------------

class TestAtomicClaimResume:
    """Verify _atomic_claim_for_resume SQL semantics via mocking.

    Real-DB concurrency is exercised manually in the §7 verification checklist
    (two concurrent curl /research/reply calls on the same paused session).
    Here we prove the function issues the correct conditional UPDATE and
    reports success/failure from rowcount.
    """

    @pytest.mark.asyncio
    async def test_returns_true_when_row_claimed(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.modules.research_agent import _atomic_claim_for_resume

        fake_result = MagicMock()
        fake_result.rowcount = 1

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=fake_result)
        mock_db.commit = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)

        with patch("app.modules.research_agent.async_session", return_value=mock_db):
            won = await _atomic_claim_for_resume("sid-123", "my reply")

        assert won is True
        mock_db.execute.assert_awaited_once()
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_row_not_claimed(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.modules.research_agent import _atomic_claim_for_resume

        fake_result = MagicMock()
        fake_result.rowcount = 0

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=fake_result)
        mock_db.commit = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)

        with patch("app.modules.research_agent.async_session", return_value=mock_db):
            won = await _atomic_claim_for_resume("sid-456", "losing reply")

        assert won is False

    @pytest.mark.asyncio
    async def test_sql_has_paused_status_guard(self):
        """Regression guard: SQL must include WHERE status = 'paused_awaiting_reply'.

        Inspects the SQL source directly rather than the TextClause object,
        making this test independent of pytest-asyncio ordering and any
        stale async_session/sqlalchemy.text patches from earlier tests.
        """
        import inspect
        from app.modules import research_agent
        source = inspect.getsource(research_agent._atomic_claim_for_resume)
        # The function body (excluding docstring) must contain the atomicity markers
        assert "UPDATE research_sessions" in source, "missing UPDATE"
        assert "WHERE id = :sid" in source, "missing id match in WHERE"
        assert "AND status = 'paused_awaiting_reply'" in source, (
            "missing status guard in WHERE — atomicity is compromised"
        )
        assert "rowcount == 1" in source, "missing rowcount check for claim success"

    @pytest.mark.asyncio
    async def test_reply_is_passed_as_parameter(self):
        """Reply text must reach the DB as a bound parameter, not string-interpolated."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.modules.research_agent import _atomic_claim_for_resume

        fake_result = MagicMock()
        fake_result.rowcount = 1

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=fake_result)
        mock_db.commit = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)

        with patch("app.modules.research_agent.async_session", return_value=mock_db):
            await _atomic_claim_for_resume("sid-param", "user reply text")

        params = mock_db.execute.await_args.args[1]
        assert params.get("sid") == "sid-param"
        assert params.get("reply") == "user reply text"


# ----------------------------------------------------------------------------
# #4 — Direct-mode exception always finalizes session
# ----------------------------------------------------------------------------

class TestDirectModeFinalization:
    """Exceptions in direct-mode helpers must land in _finalize_session."""

    @pytest.mark.asyncio
    async def test_github_mode_failure_finalizes_with_error(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import run_research

        finalize_mock = AsyncMock()
        # fetch_repo_content raises a generic RuntimeError (not a GitHub* exception)
        async def _raise(*a, **kw):
            raise RuntimeError("network fail")

        with patch("app.modules.research_agent._guard_concurrent",
                   new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session",
                   new_callable=AsyncMock, return_value="sess-gh-fail"), \
             patch("app.modules.research_agent._finalize_session", finalize_mock), \
             patch("app.utils.github_ingest.fetch_repo_content", new=AsyncMock(side_effect=_raise)):

            events = []
            async for sse in run_research("github:foo/bar"):
                events.append(sse)

        # finalize called with status='failed' and a non-None error_message
        assert finalize_mock.await_count >= 1
        call_args = finalize_mock.await_args_list[-1]
        assert call_args.args[1] == "failed"
        err = call_args.kwargs.get("error_message") or (
            call_args.args[4] if len(call_args.args) > 4 else None
        )
        assert err is not None and "network fail" in err

        # error SSE was emitted
        assert any("event: error" in e for e in events)

    @pytest.mark.asyncio
    async def test_url_mode_robots_denied_finalizes(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import run_research

        finalize_mock = AsyncMock()
        with patch("app.modules.research_agent._guard_concurrent",
                   new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session",
                   new_callable=AsyncMock, return_value="sess-url-robots"), \
             patch("app.modules.research_agent._finalize_session", finalize_mock), \
             patch("app.modules.research_agent._robots_allowed",
                   new_callable=AsyncMock, return_value=False):

            events = []
            async for sse in run_research("https://example.com/blocked"):
                events.append(sse)

        assert finalize_mock.await_count >= 1
        assert finalize_mock.await_args_list[-1].args[1] == "failed"
        assert any("event: error" in e for e in events)


# ----------------------------------------------------------------------------
# #5 — error_message propagates on topic-mode failure
# ----------------------------------------------------------------------------

class TestRunResearchErrorMessage:
    @pytest.mark.asyncio
    async def test_topic_mode_exception_includes_error_message(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import run_research

        finalize_mock = AsyncMock()
        async def _boom(*a, **kw):
            raise ValueError("decompose blew up")

        with patch("app.modules.research_agent._guard_concurrent",
                   new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session",
                   new_callable=AsyncMock, return_value="sess-topic-fail"), \
             patch("app.modules.research_agent._finalize_session", finalize_mock), \
             patch("app.modules.research_agent._decompose_topic",
                   new_callable=AsyncMock, side_effect=_boom):

            events = []
            async for sse in run_research("some topic", depth="shallow"):
                events.append(sse)

        call = finalize_mock.await_args_list[-1]
        assert call.args[1] == "failed"
        err = call.kwargs.get("error_message") or (
            call.args[4] if len(call.args) > 4 else None
        )
        assert err is not None
        assert "ValueError" in err and "decompose blew up" in err


# ----------------------------------------------------------------------------
# #6 — LLM-provided confidence resolution
# ----------------------------------------------------------------------------

class TestResolveConfidence:
    def test_uses_llm_value_when_valid(self):
        from app.modules.research_agent import _resolve_confidence
        assert _resolve_confidence(0.42, "https://reddit.com/x") == 0.42

    def test_uses_llm_boundary_values(self):
        from app.modules.research_agent import _resolve_confidence
        assert _resolve_confidence(0.0, "https://anywhere.com") == 0.0
        assert _resolve_confidence(1.0, "https://anywhere.com") == 1.0

    def test_falls_back_on_out_of_range(self):
        """Out-of-range LLM value → URL heuristic (and logs warning)."""
        from app.modules.research_agent import _resolve_confidence
        # arxiv.org is 0.95 in DOMAIN_SCORES
        assert _resolve_confidence(1.5, "https://arxiv.org/abs/123") == 0.95
        assert _resolve_confidence(-0.2, "https://arxiv.org/abs/123") == 0.95

    def test_falls_back_on_non_numeric(self):
        from app.modules.research_agent import _resolve_confidence
        # default = 0.50 for unknown domains
        assert _resolve_confidence(None, "https://nobody-knows.xyz") == 0.50
        assert _resolve_confidence("high", "https://nobody-knows.xyz") == 0.50


# ----------------------------------------------------------------------------
# #52 — _await_with_heartbeat helper
# ----------------------------------------------------------------------------

class TestAwaitWithHeartbeat:
    @pytest.mark.asyncio
    async def test_yields_heartbeats_until_task_done(self):
        from unittest.mock import patch
        from app.modules.research_agent import _await_with_heartbeat

        async def _slow():
            await asyncio.sleep(0.05)
            return "done"

        task = asyncio.create_task(_slow())
        yields = []
        # interval 0 so we don't wait; the generator still only yields while task is pending
        async for hb in _await_with_heartbeat(task, {"status": "working"}, interval=0):
            yields.append(hb)
            if len(yields) > 50:
                break  # safety

        result = task.result()
        assert result == "done"
        assert all("event: heartbeat" in y for y in yields)
        assert all('"status": "working"' in y for y in yields)

    @pytest.mark.asyncio
    async def test_yields_nothing_when_task_already_done(self):
        from app.modules.research_agent import _await_with_heartbeat

        async def _fast():
            return "instant"

        task = asyncio.create_task(_fast())
        await task  # let it complete before heartbeat starts

        yields = []
        async for hb in _await_with_heartbeat(task, {"status": "x"}, interval=0):
            yields.append(hb)

        assert yields == []


# ----------------------------------------------------------------------------
# #50 — _execute_iteration_loop helper
# ----------------------------------------------------------------------------

class TestExecuteIterationLoop:
    @pytest.mark.asyncio
    async def test_mutates_state_and_completes_without_pause(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import _execute_iteration_loop, ResearchState

        state = ResearchState(topic="t", depth="shallow")
        state.outline_facets = ["f"]

        with patch("app.modules.research_agent._search_queries",
                   new_callable=AsyncMock,
                   return_value=[{"url": "http://a", "title": "t", "content": "c", "facet": "f"}]), \
             patch("app.modules.research_agent._extract_entries",
                   new_callable=AsyncMock,
                   return_value=[{"title": "T", "content": "C", "source": "http://a", "facet": "f"}]), \
             patch("app.modules.research_agent._analyze_gaps",
                   new_callable=AsyncMock,
                   return_value={"coverage_pct": 100, "gap_queries": []}), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent.ingest_entries",
                   new_callable=AsyncMock,
                   return_value={"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 0}):

            events = []
            async for sse in _execute_iteration_loop(
                state=state, session_id="sess",
                initial_queries=[{"query": "q", "facet": "f"}],
                decompose_model="m", extract_model="m",
                topic="t", allow_pause=True,
            ):
                events.append(sse)

        assert state.iteration == 1  # shallow depth
        assert state.paused is False
        assert state.total_new == 1
        assert state.total_ingested == 1
        assert len(state.all_entries) == 1

    @pytest.mark.asyncio
    async def test_sets_paused_flag_when_gap_requests_clarification(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import _execute_iteration_loop, ResearchState

        state = ResearchState(topic="t", depth="medium")  # 2 iterations max
        state.outline_facets = ["f"]

        with patch("app.modules.research_agent._search_queries",
                   new_callable=AsyncMock,
                   return_value=[{"url": "http://a", "title": "t", "content": "c", "facet": "f"}]), \
             patch("app.modules.research_agent._extract_entries",
                   new_callable=AsyncMock,
                   return_value=[{"title": "T", "content": "C", "source": "http://a", "facet": "f"}]), \
             patch("app.modules.research_agent._analyze_gaps",
                   new_callable=AsyncMock,
                   return_value={
                       "coverage_pct": 40,
                       "gap_queries": [{"query": "more", "facet": "f"}],
                       "needs_clarification": True,
                       "clarifying_question": "Which DB engine?",
                   }), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._pause_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.ingest_entries",
                   new_callable=AsyncMock,
                   return_value={"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 0}):

            events = []
            async for sse in _execute_iteration_loop(
                state=state, session_id="sess",
                initial_queries=[{"query": "q", "facet": "f"}],
                decompose_model="m", extract_model="m",
                topic="t", allow_pause=True,
            ):
                events.append(sse)

        assert state.paused is True
        assert any("event: awaiting_reply" in e for e in events)
        assert any("Which DB engine?" in e for e in events)


# ----------------------------------------------------------------------------
# #51 + #57 — _ingest_and_finalize_direct helper + content_truncated SSE
# ----------------------------------------------------------------------------

class TestIngestAndFinalizeDirect:
    @pytest.mark.asyncio
    async def test_emits_content_truncated_when_over_cap(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import _ingest_and_finalize_direct, ResearchState

        state = ResearchState(topic="t", depth="direct_github", domain="eng")
        state.iteration = 1
        big = "x" * 50000
        entries = [{"title": "T", "content": big, "source": "s", "facet": "f"}]

        with patch("app.modules.research_agent.ingest_entries",
                   new_callable=AsyncMock,
                   return_value={"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 0}), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.settings") as s:
            s.research_max_entry_chars = 8000

            events = []
            async for sse in _ingest_and_finalize_direct(
                state=state, session_id="sess", entries=entries,
                mode="github", topic="github:x/y", t0=0.0,
            ):
                events.append(sse)

        assert any("event: content_truncated" in e for e in events)
        # content actually truncated in place
        assert len(entries[0]["content"]) == 8000

    @pytest.mark.asyncio
    async def test_unified_payload_has_common_keys(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import _ingest_and_finalize_direct, ResearchState

        state = ResearchState(topic="t", depth="direct_openapi", domain="eng")
        state.iteration = 1
        entries = [{"title": "T", "content": "short", "source": "s", "facet": "f"}]

        with patch("app.modules.research_agent.ingest_entries",
                   new_callable=AsyncMock,
                   return_value={"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 0}), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock):

            events = []
            async for sse in _ingest_and_finalize_direct(
                state=state, session_id="abc123", entries=entries,
                mode="openapi", topic="openapi:x", t0=0.0,
                extra_complete_fields={"spec_title": "Spec X"},
            ):
                events.append(sse)

        complete = [e for e in events if "event: research_complete" in e]
        assert len(complete) == 1
        payload = complete[0]

        # Common keys (#141)
        for key in [
            '"session_id": "abc123"',
            '"mode": "openapi"',
            '"domain": "eng"',
            '"depth": "direct_openapi"',
            '"iterations": 1',
            '"total_entries": 1',
            '"new": 1',
            '"versioned": 0',
            '"rejected": 0',
            '"skipped_hash": 0',
        ]:
            assert key in payload, f"missing key in payload: {key}"

        # Mode-specific extra preserved
        assert '"spec_title": "Spec X"' in payload
