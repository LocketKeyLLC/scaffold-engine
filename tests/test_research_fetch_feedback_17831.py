"""§17.831 (plan 8.1) — research fetch/summary feedback.

Covers the four 8.1 sub-items:
  - `_fetch_url_bounded` failure-reason reporting (http_NNN / size cap /
    timeout / SSRF) via the optional ``failure`` dict
  - `_fetch_and_extract` live progress counters
  - `_await_extract_with_fetch_progress` emitting real `research_fetch` SSE
    frames while the extract task runs
  - `_generate_summary` fallback distinguishing timeout vs dead-model vs
    empty-content (stamped on ``state.summary_fallback``) and the
    `research_complete` payload carrying ``fetch_stats`` + ``summary_fallback``
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.modules import research_agent
from app.modules.research_agent import (
    _await_extract_with_fetch_progress,
    _build_research_complete_payload,
    _fetch_and_extract,
    _generate_summary,
)
from app.modules.research_extractors import _fetch_url_bounded
from app.modules.research_state import ResearchState

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# _fetch_url_bounded failure reasons
# ---------------------------------------------------------------------------

def _stream_client(resp) -> MagicMock:
    """Client whose .stream() async-context-manages to ``resp``."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    client = MagicMock()
    client.stream = MagicMock(return_value=cm)
    return client


def _resp(status=200, headers=None, chunks=(b"",), url="http://1.1.1.1/x"):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.url = url
    resp.encoding = "utf-8"

    async def _aiter():
        for c in chunks:
            yield c

    resp.aiter_bytes = _aiter
    return resp


class TestFetchFailureReasons:
    async def test_non_200_reports_http_status(self):
        client = _stream_client(_resp(status=404))
        fail: dict = {}
        with patch("app.modules.research_agent.get_generic_http_client",
                   return_value=client):
            out = await _fetch_url_bounded("http://1.1.1.1/x", failure=fail)
        assert out is None
        assert fail["reason"] == "http_404"

    async def test_streamed_cap_reports_size_cap(self):
        client = _stream_client(_resp(chunks=(b"x" * 64,)))
        fail: dict = {}
        with patch("app.modules.research_agent.get_generic_http_client",
                   return_value=client):
            out = await _fetch_url_bounded(
                "http://1.1.1.1/x", max_bytes=10, failure=fail,
            )
        assert out is None
        assert fail["reason"] == "size_cap_exceeded"

    async def test_content_length_header_reports_size_cap(self):
        client = _stream_client(_resp(headers={"content-length": "99999"}))
        fail: dict = {}
        with patch("app.modules.research_agent.get_generic_http_client",
                   return_value=client):
            out = await _fetch_url_bounded(
                "http://1.1.1.1/x", max_bytes=10, failure=fail,
            )
        assert out is None
        assert fail["reason"] == "size_cap_exceeded"

    async def test_timeout_reports_timeout(self):
        client = MagicMock()
        client.stream = MagicMock(side_effect=httpx.ConnectTimeout("slow"))
        fail: dict = {}
        with patch("app.modules.research_agent.get_generic_http_client",
                   return_value=client):
            out = await _fetch_url_bounded("http://1.1.1.1/x", failure=fail)
        assert out is None
        assert fail["reason"] == "timeout"

    async def test_ssrf_reject_reports_reason(self):
        fail: dict = {}
        out = await _fetch_url_bounded("http://localhost/x", failure=fail)
        assert out is None
        assert fail["reason"] == "ssrf_rejected"

    async def test_no_failure_dict_is_fine(self):
        """Back-compat: callers that pass nothing see the old behavior."""
        out = await _fetch_url_bounded("http://localhost/x")
        assert out is None

    async def test_success_leaves_failure_empty(self):
        client = _stream_client(_resp(chunks=(b"hello world",)))
        fail: dict = {}
        with patch("app.modules.research_agent.get_generic_http_client",
                   return_value=client):
            out = await _fetch_url_bounded("http://1.1.1.1/x", failure=fail)
        assert out == "hello world"
        assert fail == {}


# ---------------------------------------------------------------------------
# _fetch_and_extract progress counters
# ---------------------------------------------------------------------------

class TestFetchProgressCounters:
    async def test_counters_track_ok_failed_and_reasons(self):
        async def fake_bounded(url, *a, failure=None, **k):
            if "bad" in url:
                if failure is not None:
                    failure["reason"] = "http_404"
                return None
            return "<html>" + "x" * 500

        progress: dict = {}
        with patch.object(research_agent, "_fetch_url_bounded",
                          side_effect=fake_bounded), \
             patch.object(research_agent.trafilatura, "extract",
                          return_value="y" * 200):
            out = await _fetch_and_extract(
                [{"url": "http://ok.example.com/a"},
                 {"url": "http://bad.example.com/b"},
                 {"url": "http://ok.example.com/c"}],
                progress=progress,
            )

        assert len(out) == 2
        assert progress["total"] == 3
        assert progress["done"] == 3
        assert progress["ok"] == 2
        assert progress["failed"] == 1
        assert progress["failed_reasons"] == {"http_404": 1}

    async def test_short_extract_counts_no_content(self):
        with patch.object(research_agent, "_fetch_url_bounded",
                          AsyncMock(return_value="<html>hi</html>")), \
             patch.object(research_agent.trafilatura, "extract",
                          return_value="too short"):
            progress: dict = {}
            out = await _fetch_and_extract(
                [{"url": "http://ok.example.com/a"}], progress=progress,
            )
        assert out == []
        assert progress["failed_reasons"] == {"no_content": 1}

    async def test_no_progress_dict_is_fine(self):
        with patch.object(research_agent, "_fetch_url_bounded",
                          AsyncMock(return_value=None)):
            out = await _fetch_and_extract([{"url": "http://x.example.com/"}])
        assert out == []


# ---------------------------------------------------------------------------
# research_fetch SSE emission
# ---------------------------------------------------------------------------

def _parse_frames(chunks: list[str]) -> list[tuple[str, dict]]:
    events = []
    for chunk in chunks:
        name, data = None, None
        for line in chunk.strip().split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        events.append((name, data))
    return events


class TestResearchFetchEmission:
    async def test_frames_emitted_while_task_runs(self, monkeypatch):
        monkeypatch.setattr(research_agent, "_FETCH_PROGRESS_POLL_S", 0.01)
        progress: dict = {}

        async def worker():
            progress.update(total=2, done=0, ok=0, failed=0,
                            last_url="", failed_reasons={})
            for i in (1, 2):
                await asyncio.sleep(0.05)
                progress["done"] = i
                progress["ok"] = i
                progress["last_url"] = f"http://x/{i}"
            await asyncio.sleep(0.05)
            return "done"

        task = asyncio.create_task(worker())
        chunks = [c async for c in _await_extract_with_fetch_progress(
            task, {"status": "extracting"}, progress, iteration=1,
        )]
        assert task.result() == "done"

        fetch_frames = [d for (n, d) in _parse_frames(chunks)
                        if n == "research_fetch"]
        assert fetch_frames, "no research_fetch frames emitted"
        assert fetch_frames[-1]["fetched"] == 2
        assert fetch_frames[-1]["total"] == 2
        assert fetch_frames[-1]["iteration"] == 1
        assert "last_url" in fetch_frames[-1]

    async def test_instant_task_emits_nothing(self):
        async def instant():
            return 42

        task = asyncio.create_task(instant())
        await asyncio.sleep(0)  # let it finish
        chunks = [c async for c in _await_extract_with_fetch_progress(
            task, {"status": "extracting"}, {}, iteration=1,
        )]
        assert chunks == []
        assert task.result() == 42


# ---------------------------------------------------------------------------
# Summary fallback reasons
# ---------------------------------------------------------------------------

class TestSummaryFallbackReasons:
    @pytest.fixture(autouse=True)
    def _plain_summary_path(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "citation_faithfulness_check_enabled", False)

    async def test_timeout_stamps_reason(self, monkeypatch):
        monkeypatch.setattr(research_agent, "_SUMMARY_PROMPT_TIMEOUT_S", 0.01)

        async def slow(*a, **k):
            await asyncio.sleep(1)

        state = ResearchState(topic="t")
        with patch.object(research_agent.model_router, "generate",
                          side_effect=slow):
            text = await _generate_summary(state)
        assert state.summary_fallback == "summary_timeout"
        assert "timed out" in text

    async def test_llm_failure_stamps_reason(self):
        resp = MagicMock(success=False, error="model exploded")
        state = ResearchState(topic="t")
        with patch.object(research_agent.model_router, "generate",
                          AsyncMock(return_value=resp)):
            text = await _generate_summary(state)
        assert state.summary_fallback == "summary_llm_failed"
        assert "model exploded" in text

    async def test_empty_twice_stamps_reason(self):
        resp = MagicMock(success=True, text="")
        state = ResearchState(topic="t")
        with patch.object(research_agent.model_router, "generate",
                          AsyncMock(return_value=resp)) as gen:
            text = await _generate_summary(state)
        assert gen.await_count == 2  # §17.559 retry-on-empty preserved
        assert state.summary_fallback == "summary_empty"
        assert "empty content" in text


# ---------------------------------------------------------------------------
# research_complete payload
# ---------------------------------------------------------------------------

class TestResearchCompletePayload:
    def test_fetch_stats_and_summary_fallback_present(self):
        state = ResearchState(topic="t")
        state.fetch_attempted = 30
        state.fetch_ok = 3
        state.fetch_failed = 27
        state.fallback_entries = 5
        state.summary_fallback = "summary_timeout"
        payload = _build_research_complete_payload(
            state, "sid", mode="topic", duration_ms=1,
        )
        assert payload["fetch_stats"] == {
            "attempted": 30, "ok": 3, "failed": 27, "fallback_entries": 5,
        }
        assert payload["summary_fallback"] == "summary_timeout"

    def test_defaults_are_zero_and_none(self):
        payload = _build_research_complete_payload(
            ResearchState(topic="t"), "sid", mode="topic", duration_ms=1,
        )
        assert payload["fetch_stats"] == {
            "attempted": 0, "ok": 0, "failed": 0, "fallback_entries": 0,
        }
        assert payload["summary_fallback"] is None
