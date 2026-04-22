"""Tests for research_agent — pure helpers: resolve_confidence, await_with_heartbeat, execute_iteration_loop, check_contradictions.

Split from the original test_research_agent.py (#9.6).
Shared imports + helpers live in _research_agent_shared.
"""
from tests._research_agent_shared import *  # noqa: F401, F403

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
