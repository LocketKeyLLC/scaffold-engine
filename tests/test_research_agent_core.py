"""Tests for research_agent — decomposition, search_queries, extract_entries, analyze_gaps, run_research top-level orchestration.

Split from the original test_research_agent.py (#9.6).
Shared imports + helpers live in _research_agent_shared.
"""
from tests._research_agent_shared import *  # noqa: F401, F403


def _make_create_task_side_effect(result):
    """Side-effect for `patch('asyncio.create_task')`.

    The production code calls ``asyncio.create_task(some_coro)``. Mocking
    via ``mock_task.return_value = future`` left ``some_coro`` un-awaited
    and un-closed — pytest then surfaced ``RuntimeWarning: coroutine
    AsyncMockMixin._execute_mock_call was never awaited`` at GC time
    (audit M7). This side-effect closes the coro deterministically and
    returns a pre-resolved future carrying ``result``.
    """
    def _side_effect(coro):
        # Closing the coroutine before the future resolves silences the
        # un-awaited warning. Order matters: close first, then return.
        coro.close()
        f = asyncio.Future()
        f.set_result(result)
        return f
    return _side_effect


class TestDecomposeTopic:
    """Tests for _decompose_topic() - LLM decomposes topic into queries."""

    @pytest.mark.asyncio
    async def test_parses_valid_json(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))
            from app.modules.research_agent import _decompose_topic

            result = await _decompose_topic("Redis caching")

            assert "queries" in result
            assert "facets" in result
            assert len(result["queries"]) == 3
            assert result["topic_complexity"] == "medium"

    @pytest.mark.asyncio
    async def test_fallback_on_bad_json(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response("not json at all"))
            from app.modules.research_agent import _decompose_topic

            result = await _decompose_topic("Redis caching")

            assert "queries" in result
            assert len(result["queries"]) >= 3
            assert result["facets"] == ["Redis caching"]

    @pytest.mark.asyncio
    async def test_fallback_on_llm_failure(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response("", success=False))
            from app.modules.research_agent import _decompose_topic

            result = await _decompose_topic("Redis caching")

            assert "queries" in result
            assert result["topic_complexity"] == "medium"

    @pytest.mark.asyncio
    async def test_existing_facets_in_prompt(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))
            from app.modules.research_agent import _decompose_topic

            await _decompose_topic(
                "Redis caching",
                existing_facets=["overview", "performance"],
                gap_focus="security aspects",
            )

            # W.6: code now uses tool_call(messages=[...]). User message is the prompt.
            messages = mock_mr.tool_call.call_args.kwargs["messages"]
            prompt_text = next(m["content"] for m in messages if m["role"] == "user")
            assert "overview" in prompt_text
            assert "security aspects" in prompt_text


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


class TestExtractEntries:
    """Tests for _extract_entries() - LLM distills results into entries."""

    @pytest.mark.asyncio
    async def test_extracts_entries(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_EXTRACTION))
            from app.modules.research_agent import _extract_entries

            entries = await _extract_entries(MOCK_SEARCH_RESULTS, "Redis caching")

            assert len(entries) == 2
            assert entries[0]["title"] == "Redis default port"
            assert "confidence_score" in entries[0]

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.tool_call = mock_mr.generate = AsyncMock()
            from app.modules.research_agent import _extract_entries

            entries = await _extract_entries([], "Redis")

            assert entries == []
            mock_mr.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response("", success=False))
            from app.modules.research_agent import _extract_entries

            entries = await _extract_entries(MOCK_SEARCH_RESULTS, "Redis")

            assert entries == []

    @pytest.mark.asyncio
    async def test_batches_large_input(self):
        # Must mock _fetch_and_extract -- otherwise the test triggers real
        # network fetches against the fake URLs (https://ex.com/{i}). In
        # isolation that resolves quickly; under load (full suite, with
        # other tests' httpx clients warmed up) it can take 30+ seconds
        # and trip the global pytest --timeout=30 limit.
        with patch("app.modules.research_agent.model_router") as mock_mr, \
             patch("app.modules.research_agent._fetch_and_extract",
                   new_callable=AsyncMock) as mock_fetch:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_EXTRACTION))
            mock_fetch.return_value = []
            from app.modules.research_agent import _extract_entries

            results = [
                {"title": f"R{i}", "url": f"https://ex.com/{i}", "content": f"Content {i}"}
                for i in range(15)
            ]
            entries = await _extract_entries(results, "Redis")

            assert mock_mr.generate.await_count == 2
            assert len(entries) == 4


class TestAnalyzeGaps:
    """Tests for _analyze_gaps() - coverage gap analysis."""

    @pytest.mark.asyncio
    async def test_parses_gap_response(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_GAP_ANALYSIS))
            from app.modules.research_agent import _analyze_gaps, ResearchState

            state = ResearchState(topic="Redis")
            state.outline_facets = ["overview", "performance", "security"]
            state.all_entries = [{"facet": "overview", "title": "T", "content": "C"}]

            result = await _analyze_gaps(state)

            assert result["coverage_pct"] == 60
            assert "security" in result["gap_facets"]
            assert len(result["gap_queries"]) == 1

    @pytest.mark.asyncio
    async def test_fallback_on_failure(self):
        with patch("app.modules.research_agent.model_router") as mock_mr:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response("garbage", success=True))
            from app.modules.research_agent import _analyze_gaps, ResearchState

            state = ResearchState(topic="Redis")
            state.outline_facets = ["overview"]
            state.all_entries = []

            result = await _analyze_gaps(state)

            assert result["coverage_pct"] == 0
            assert result["reason"] == "gap_analysis_failed"
            assert result["gap_queries"] == []


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
             patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("test-session-id", None)), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("test-session-id", None)), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.create_task") as mock_task:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))

            mock_task.side_effect = _make_create_task_side_effect("Summary text.")

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
             patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("test-session-id", None)), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("test-session-id", None)), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.create_task") as mock_task:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))

            mock_task.side_effect = _make_create_task_side_effect("Summary text.")

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
             patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("test-session-id", None)), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("test-session-id", None)), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.create_task") as mock_task:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))

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
             patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("test-session-id", None)), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("test-session-id", None)), \
             patch("app.modules.research_agent._update_session_iteration", new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.modules.research_agent.asyncio.create_task") as mock_task:
            mock_mr.tool_call = mock_mr.generate = AsyncMock(return_value=_make_generate_response(GOOD_DECOMPOSITION))

            mock_task.side_effect = _make_create_task_side_effect("No data.")

            from app.modules.research_agent import run_research

            events_raw = []
            async for sse in run_research("Redis", depth="medium"):
                events_raw.append(sse)

            events = self._parse_events(events_raw)
            iter_complete = [e for e in events if e["type"] == "iteration_complete"]
            assert len(iter_complete) == 1
            assert iter_complete[0]["data"]["reason"] == "no_results"
