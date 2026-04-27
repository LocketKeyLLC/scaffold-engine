"""Tests for research_agent — ingestion breakdown counters + ingest_and_finalize_direct.

Split from the original test_research_agent.py (#9.6).
Shared imports + helpers live in _research_agent_shared.
"""
from tests._research_agent_shared import *  # noqa: F401, F403

class TestIngestionBreakdown:
    """Verify /research surfaces the three-bucket ingest classification."""

    @pytest.mark.asyncio
    async def test_research_complete_contains_breakdown_fields(self):
        """Final research_complete SSE includes new, versioned, rejected, skipped_hash."""
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import run_research

        fake_entries = [{"content": "fact", "facet": "x", "source": "http://a"}]
        fake_stats = {"new": 3, "versioned": 2, "rejected": 1, "skipped_hash": 4}

        with patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("sess-1", None)), \
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

        with patch("app.modules.research_agent._guard_and_create_session", new_callable=AsyncMock, return_value=("sess-2", None)), \
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
