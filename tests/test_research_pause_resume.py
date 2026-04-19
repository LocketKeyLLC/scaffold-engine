"""Phase 2: pause + resume mechanics for /research.

All tests mock DB, LLM, and ingest layer. Verifies:
  - Pause emission conditional on LLM signal + remaining iterations
  - _pause_session writes correct DB state
  - _rehydrate_state reconstructs ResearchState from snapshot
  - resume_research rejects non-paused / expired / empty-reply sessions
  - resume_research emits research_resumed + completes
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import research_agent as ra


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _drain(gen):
    """Collect all SSE events from an async generator."""
    events = []
    async for e in gen:
        events.append(e)
    return events


def _parse_events(raw: list[str]) -> list[dict]:
    """Parse SSE strings into {event, data} dicts."""
    out = []
    for s in raw:
        lines = s.strip().split("\n")
        evt = next((l[len("event: "):] for l in lines if l.startswith("event: ")), "")
        data_line = next((l[len("data: "):] for l in lines if l.startswith("data: ")), "{}")
        out.append({"event": evt, "data": json.loads(data_line)})
    return out


# ---------------------------------------------------------------------------
# _rehydrate_state
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestRehydrateState:
    def test_roundtrip_core_fields(self):
        row = {
            "topic": "vector databases",
            "depth": "medium",
            "domain": "rag",
            "state_snapshot": {
                "iteration": 1,
                "search_history": ["q1", "q2"],
                "url_history": ["https://a.test", "https://b.test"],
                "entries_projection": [{"title": "t1", "content_hash": "h1"}],
                "outline_facets": ["f1", "f2"],
                "covered_facets": ["f1"],
                "gap_queries": [{"query": "g1", "facet": "f2"}],
                "totals": {"ingested": 5, "rejected": 1, "new": 4,
                           "versioned": 1, "skipped_hash": 0},
            },
        }
        state = ra._rehydrate_state(row)
        assert state.topic == "vector databases"
        assert state.depth == "medium"
        assert state.domain == "rag"
        assert state.iteration == 1
        assert state.search_history == {"q1", "q2"}
        assert state.url_history == {"https://a.test", "https://b.test"}
        assert state.outline_facets == ["f1", "f2"]
        assert state.covered_facets == {"f1"}
        assert len(state.all_entries) == 1
        assert state.total_ingested == 5
        assert state.total_new == 4

    def test_empty_snapshot_defaults(self):
        row = {"topic": "t", "depth": "shallow", "domain": "eng",
               "state_snapshot": {}}
        state = ra._rehydrate_state(row)
        assert state.iteration == 0
        assert state.search_history == set()
        assert state.total_ingested == 0

    def test_json_string_snapshot_parsed(self):
        row = {"topic": "t", "depth": "shallow", "domain": "eng",
               "state_snapshot": json.dumps({"iteration": 3})}
        state = ra._rehydrate_state(row)
        assert state.iteration == 3


# ---------------------------------------------------------------------------
# resume_research — guard paths
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestResumeGuards:
    def test_missing_session_yields_404(self):
        with patch.object(ra, "_load_session_for_resume",
                          AsyncMock(return_value=None)):
            events = _parse_events(_run(_drain(
                ra.resume_research("does-not-exist", "hello")
            )))
        assert len(events) == 1
        assert events[0]["event"] == "error"
        assert events[0]["data"]["http_status"] == 404

    def test_wrong_status_yields_409(self):
        row = {"id": "s1", "topic": "t", "depth": "shallow", "domain": "eng",
               "status": "running", "state_snapshot": {},
               "pause_question": None, "pause_expires_at": None,
               "pause_reply": None}
        with patch.object(ra, "_load_session_for_resume",
                          AsyncMock(return_value=row)):
            events = _parse_events(_run(_drain(
                ra.resume_research("s1", "hello")
            )))
        assert events[0]["event"] == "error"
        assert events[0]["data"]["http_status"] == 409

    def test_expired_pause_cancelled_410(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        row = {"id": "s1", "topic": "t", "depth": "shallow", "domain": "eng",
               "status": "paused_awaiting_reply", "state_snapshot": {},
               "pause_question": "q?", "pause_expires_at": past,
               "pause_reply": None}
        finalize = AsyncMock()
        with patch.object(ra, "_load_session_for_resume",
                          AsyncMock(return_value=row)), \
             patch.object(ra, "_finalize_session", finalize):
            events = _parse_events(_run(_drain(
                ra.resume_research("s1", "hello")
            )))
        assert events[0]["event"] == "error"
        assert events[0]["data"]["http_status"] == 410
        finalize.assert_awaited_once()
        args, kwargs = finalize.call_args
        # 2nd positional is status
        assert args[1] == "cancelled"

    def test_empty_reply_rejected_400(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        row = {"id": "s1", "topic": "t", "depth": "shallow", "domain": "eng",
               "status": "paused_awaiting_reply", "state_snapshot": {},
               "pause_question": "q?", "pause_expires_at": future,
               "pause_reply": None}
        with patch.object(ra, "_load_session_for_resume",
                          AsyncMock(return_value=row)):
            events = _parse_events(_run(_drain(
                ra.resume_research("s1", "   ")
            )))
        assert events[0]["event"] == "error"
        assert events[0]["data"]["http_status"] == 400


# ---------------------------------------------------------------------------
# resume_research — happy path (mocked loop internals)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestResumeHappyPath:
    def test_resumed_event_emitted(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        row = {
            "id": "s1", "topic": "gRPC tracing", "depth": "shallow",
            "domain": "eng", "status": "paused_awaiting_reply",
            "state_snapshot": {
                "iteration": 1,
                "search_history": [], "url_history": [],
                "entries_projection": [], "outline_facets": ["tracing"],
                "covered_facets": [], "gap_queries": [],
                "totals": {"ingested": 0, "rejected": 0, "new": 0,
                           "versioned": 0, "skipped_hash": 0},
            },
            "pause_question": "OTel or Jaeger?",
            "pause_expires_at": future, "pause_reply": None,
        }

        # Session update during resume + final update/finalize
        fake_db = MagicMock()
        fake_db.execute = AsyncMock()
        fake_db.commit = AsyncMock()

        class _AsyncCM:
            async def __aenter__(self_inner): return fake_db
            async def __aexit__(self_inner, *a): return False

        # depth=shallow → max_iterations=1 → loop body skipped, goes straight
        # to summary + finalize. This exercises the state machine edges
        # without needing to mock search/extract/ingest.
        async def _fake_summary(*_a, **_kw):
            return "resumed-summary"

        with patch.object(ra, "_load_session_for_resume",
                          AsyncMock(return_value=row)), \
             patch.object(ra, "async_session", lambda: _AsyncCM()), \
             patch.object(ra, "_atomic_claim_for_resume",
                          AsyncMock(return_value=True)), \
             patch.object(ra, "_decompose_topic", AsyncMock(return_value={
                 "topic_complexity": "medium",
                 "facets": ["tracing"],
                 "queries": [{"query": "gRPC tracing",
                              "facet": "tracing",
                              "search_category": "general"}],
             })), \
             patch.object(ra, "_search_queries",
                          AsyncMock(return_value=[])), \
             patch.object(ra, "_update_session_iteration", AsyncMock()), \
             patch.object(ra, "_finalize_session", AsyncMock()), \
             patch.object(ra, "_generate_summary", _fake_summary):
            events = _parse_events(_run(_drain(
                ra.resume_research("s1", "OTel please")
            )))

        kinds = [e["event"] for e in events]
        assert "research_resumed" in kinds
        assert "research_complete" in kinds
        resumed = next(e for e in events if e["event"] == "research_resumed")
        assert resumed["data"]["reply"] == "OTel please"
        assert resumed["data"]["session_id"] == "s1"
        complete = next(e for e in events if e["event"] == "research_complete")
        assert complete["data"]["resumed_from_pause"] is True


# ---------------------------------------------------------------------------
# Pause gate in run_research — only fires when LLM signals + iterations remain
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestPauseGateInLoop:
    """End-to-end test of the pause condition. We mock the LLM to return a
    clarification request and verify awaiting_reply is emitted (and the loop
    stops before summary/finalize runs)."""

    def _setup_patches(self, stack, gaps_response: dict):
        stack.enter_context(patch.object(
            ra, "_guard_concurrent", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(
            ra, "_create_session", AsyncMock(return_value="sid-1")))
        stack.enter_context(patch.object(
            ra, "_decompose_topic",
            AsyncMock(return_value={
                "topic_complexity": "medium",
                "facets": ["a", "b"],
                "queries": [{"query": "a", "facet": "a",
                             "search_category": "general"}],
            })))
        stack.enter_context(patch.object(
            ra, "_search_queries",
            AsyncMock(return_value=[{"title": "r1", "url": "https://u",
                                     "content": "snippet", "facet": "a"}])))
        stack.enter_context(patch.object(
            ra, "_extract_entries",
            AsyncMock(return_value=[{"title": "e1", "content": "c",
                                     "source": "https://u",
                                     "confidence_score": 0.8,
                                     "source_type": "community",
                                     "facet": "a"}])))
        stack.enter_context(patch.object(
            ra, "ingest_entries",
            AsyncMock(return_value={"new": 1, "versioned": 0,
                                    "rejected": 0, "skipped_hash": 0})))
        stack.enter_context(patch.object(
            ra, "_analyze_gaps", AsyncMock(return_value=gaps_response)))
        stack.enter_context(patch.object(
            ra, "_update_session_iteration", AsyncMock()))
        stack.enter_context(patch.object(
            ra, "_pause_session", AsyncMock()))
        stack.enter_context(patch.object(
            ra, "_finalize_session", AsyncMock()))
        # _generate_summary would run if loop completes — mock defensively
        stack.enter_context(patch.object(
            ra, "_generate_summary", AsyncMock(return_value="s")))

    def test_pause_emitted_when_llm_requests(self):
        from contextlib import ExitStack
        gaps = {
            "coverage_pct": 60,
            "covered_facets": ["a"],
            "gap_facets": ["b"],
            "gap_queries": [{"query": "b stuff", "facet": "b"}],
            "assessment": "ambiguous",
            "needs_clarification": True,
            "clarifying_question": "OTel or Jaeger?",
        }
        with ExitStack() as stack:
            self._setup_patches(stack, gaps)
            events = _parse_events(_run(_drain(
                ra.run_research("tracing", depth="medium")
            )))

        kinds = [e["event"] for e in events]
        assert "awaiting_reply" in kinds
        # Must NOT reach research_complete — the loop returned at pause.
        assert "research_complete" not in kinds
        pause_evt = next(e for e in events if e["event"] == "awaiting_reply")
        assert pause_evt["data"]["question"] == "OTel or Jaeger?"
        assert pause_evt["data"]["expires_in_seconds"] == 3600
        assert pause_evt["data"]["session_id"] == "sid-1"

    def test_no_pause_when_flag_false(self):
        from contextlib import ExitStack
        gaps = {
            "coverage_pct": 100,  # triggers coverage_threshold convergence
            "covered_facets": ["a", "b"],
            "gap_facets": [],
            "gap_queries": [],
            "assessment": "complete",
            "needs_clarification": False,
            "clarifying_question": "",
        }
        with ExitStack() as stack:
            self._setup_patches(stack, gaps)
            events = _parse_events(_run(_drain(
                ra.run_research("tracing", depth="medium")
            )))
        kinds = [e["event"] for e in events]
        assert "awaiting_reply" not in kinds
        assert "research_complete" in kinds

    def test_no_pause_when_question_empty(self):
        """needs_clarification=True but empty question → no pause (guard)."""
        from contextlib import ExitStack
        gaps = {
            "coverage_pct": 100,
            "covered_facets": ["a", "b"],
            "gap_facets": [],
            "gap_queries": [],
            "assessment": "complete",
            "needs_clarification": True,
            "clarifying_question": "   ",
        }
        with ExitStack() as stack:
            self._setup_patches(stack, gaps)
            events = _parse_events(_run(_drain(
                ra.run_research("tracing", depth="medium")
            )))
        kinds = [e["event"] for e in events]
        assert "awaiting_reply" not in kinds


# ---------------------------------------------------------------------------
# _pause_session writes correct DB state
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestPauseSession:
    def test_writes_paused_status_and_snapshot(self):
        state = ra.ResearchState(topic="t", depth="shallow", domain="eng")
        state.iteration = 1
        state.search_history.add("q1")
        state.url_history.add("https://u")

        fake_db = MagicMock()
        fake_db.execute = AsyncMock()
        fake_db.commit = AsyncMock()

        class _AsyncCM:
            async def __aenter__(self_inner): return fake_db
            async def __aexit__(self_inner, *a): return False

        with patch.object(ra, "async_session", lambda: _AsyncCM()):
            _run(ra._pause_session("sid", state, "is it X or Y?"))

        fake_db.execute.assert_awaited_once()
        call = fake_db.execute.call_args
        # second positional arg is the params dict
        params = call.args[1]
        assert params["sid"] == "sid"
        assert params["question"] == "is it X or Y?"
        assert params["ttl"] == 3600
        snap = json.loads(params["snapshot"])
        assert snap["iteration"] == 1
        assert "q1" in snap["search_history"]
        assert "https://u" in snap["url_history"]
        fake_db.commit.assert_awaited_once()
