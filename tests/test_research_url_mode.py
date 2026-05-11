"""Behavioral tests for /research <url> URL-mode branch."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import research_agent as ra
from app.providers.base import ToolCall


def _llm_with_entries(entries_json: str):
    """Fake response that satisfies both generate (.text) and W.6
    tool_call (.tool_calls[0].arguments['entries']) read paths."""
    parsed = json.loads(entries_json) if entries_json else []
    return MagicMock(
        success=True, text=entries_json, error=None,
        tool_calls=[ToolCall(id="t0", name="record_entries",
                              arguments={"entries": parsed})],
    )


# ---------------------------------------------------------------------------
# _is_url
# ---------------------------------------------------------------------------

class TestIsUrl:
    def test_http_url(self):
        assert ra._is_url("http://example.com/page")

    def test_https_url(self):
        assert ra._is_url("https://docs.python.org/3/")

    def test_https_with_path_query(self):
        assert ra._is_url("https://a.b.com/x/y?z=1#f")

    def test_bare_domain_rejected(self):
        assert not ra._is_url("example.com")

    def test_topic_rejected(self):
        assert not ra._is_url("HNSW index tuning for Milvus")

    def test_empty_rejected(self):
        assert not ra._is_url("")

    def test_ftp_rejected(self):
        assert not ra._is_url("ftp://example.com")

    def test_whitespace_tolerated(self):
        assert ra._is_url("  https://example.com  ")


# ---------------------------------------------------------------------------
# _robots_allowed — fail-open on missing/errored robots.txt
# ---------------------------------------------------------------------------

class TestRobotsAllowed:
    @pytest.mark.asyncio
    async def test_allowed_when_robots_404(self):
        mock_resp = MagicMock(status_code=404, text="")
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch.object(ra, "get_generic_http_client", return_value=mock_client):
            assert await ra._robots_allowed("https://example.com/page") is True

    @pytest.mark.asyncio
    async def test_allowed_when_exception(self):
        with patch.object(ra, "get_generic_http_client", side_effect=Exception("network down")):
            assert await ra._robots_allowed("https://example.com/page") is True

    @pytest.mark.asyncio
    async def test_disallowed_by_robots(self):
        robots_txt = "User-agent: *\nDisallow: /private/"
        mock_resp = MagicMock(status_code=200, text=robots_txt)
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch.object(ra, "get_generic_http_client", return_value=mock_client):
            allowed = await ra._robots_allowed("https://example.com/private/secret")
            assert allowed is False

    @pytest.mark.asyncio
    async def test_allowed_by_robots(self):
        robots_txt = "User-agent: *\nDisallow: /private/"
        mock_resp = MagicMock(status_code=200, text=robots_txt)
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch.object(ra, "get_generic_http_client", return_value=mock_client):
            allowed = await ra._robots_allowed("https://example.com/public/page")
            assert allowed is True


# ---------------------------------------------------------------------------
# _fetch_url_bounded — byte cap via content-length + streaming
# ---------------------------------------------------------------------------

class TestFetchUrlBounded:
    @pytest.mark.asyncio
    async def test_rejects_oversize_content_length(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": str(10 * 1024 * 1024)}  # 10 MB
        # §17.93 — set resp.url to the original URL so the redirect re-check
        # is skipped (no redirect simulated here).
        mock_resp.url = "https://example.com/big"

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_resp
        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream_cm)

        with patch.object(ra, "get_generic_http_client", return_value=mock_client):
            result = await ra._fetch_url_bounded("https://example.com/big")
            assert result is None

    @pytest.mark.asyncio
    async def test_fetches_small_page(self):
        body = b"<html><body>hello world</body></html>"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": str(len(body))}
        mock_resp.encoding = "utf-8"
        mock_resp.url = "https://example.com/small"

        async def _chunks():
            yield body
        mock_resp.aiter_bytes = _chunks

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_resp
        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream_cm)

        with patch.object(ra, "get_generic_http_client", return_value=mock_client):
            result = await ra._fetch_url_bounded("https://example.com/small")
            assert result == "<html><body>hello world</body></html>"

    @pytest.mark.asyncio
    async def test_rejects_non_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.headers = {}
        mock_resp.url = "https://example.com/err"

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_resp
        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream_cm)

        with patch.object(ra, "get_generic_http_client", return_value=mock_client):
            result = await ra._fetch_url_bounded("https://example.com/err")
            assert result is None

    @pytest.mark.asyncio
    async def test_mid_stream_cap(self):
        big_chunk = b"x" * (6 * 1024 * 1024)  # 6 MB, one chunk
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}  # no content-length
        mock_resp.encoding = "utf-8"
        mock_resp.url = "https://example.com/streaming"

        async def _chunks():
            yield big_chunk
        mock_resp.aiter_bytes = _chunks

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_resp
        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream_cm)

        with patch.object(ra, "get_generic_http_client", return_value=mock_client):
            result = await ra._fetch_url_bounded("https://example.com/streaming")
            assert result is None


# ---------------------------------------------------------------------------
# run_research — URL-mode E2E behavioral
# ---------------------------------------------------------------------------

def _parse_sse(raw_events: list[str]) -> list[tuple[str, dict]]:
    out = []
    for blob in raw_events:
        etype = None
        data = ""
        for line in blob.splitlines():
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if etype:
            try:
                out.append((etype, json.loads(data) if data else {}))
            except json.JSONDecodeError:
                out.append((etype, {}))
    return out


class TestRunResearchUrlMode:
    @pytest.mark.asyncio
    async def test_url_mode_happy_path(self):
        """URL topic -> skips decompose/search, reaches research_complete."""
        fake_resp = _llm_with_entries('[{"title":"T","content":"body content here long enough","tags":"","source":"https://example.com/page","source_type":"community"}]')

        with patch.object(ra, "_guard_and_create_session", AsyncMock(return_value=(str(42), None))), \
             patch.object(ra, "_robots_allowed", AsyncMock(return_value=True)), \
             patch.object(ra, "_fetch_url_bounded", AsyncMock(return_value="<html>x</html>")), \
             patch("asyncio.to_thread", AsyncMock(return_value="Clean article body " * 30)), \
             patch.object(ra.model_router, "generate", AsyncMock(return_value=fake_resp)), \
             patch.object(ra.model_router, "tool_call", AsyncMock(return_value=fake_resp)), \
             patch.object(ra, "ingest_entries", AsyncMock(return_value={"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 0})), \
             patch.object(ra, "_generate_summary", AsyncMock(return_value="summary text")), \
             patch.object(ra, "_update_session_iteration", AsyncMock()), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            events = []
            async for blob in ra.run_research("https://example.com/page", depth="medium"):
                events.append(blob)

            parsed = _parse_sse(events)
            etypes = [e for e, _ in parsed]

            assert "research_started" in etypes
            assert "research_complete" in etypes
            started = dict(parsed)["research_started"]
            assert started.get("mode") == "direct_url"
            complete = dict(parsed)["research_complete"]
            assert complete["depth"] == "direct_url"
            assert complete["iterations"] == 1

    @pytest.mark.asyncio
    async def test_url_mode_classifier_bypass_skips_llm(self):
        """§17.112 — A URL that classifies to a curated source_type (e.g.,
        a Stack Overflow answer) must NOT trigger the LLM extract loop.
        Entries are built directly from chunks with the classified
        source_type + §17.104 provenance.
        """
        tool_call_mock = AsyncMock(return_value=_llm_with_entries(
            '[{"title":"X","content":"x","tags":"","source":"x","source_type":"x"}]'
        ))
        ingest_seen: list[list[dict]] = []

        async def _capture_ingest(entries, **_):
            ingest_seen.append(list(entries))
            return {"new": len(entries), "versioned": 0, "rejected": 0, "skipped_hash": 0}

        with patch.object(ra, "_guard_and_create_session", AsyncMock(return_value=(str(101), None))), \
             patch.object(ra, "_robots_allowed", AsyncMock(return_value=True)), \
             patch.object(ra, "_fetch_url_bounded", AsyncMock(return_value="<html>x</html>")), \
             patch("asyncio.to_thread", AsyncMock(return_value="Stack Overflow answer body " * 30)), \
             patch.object(ra.model_router, "tool_call", tool_call_mock), \
             patch.object(ra, "ingest_entries", AsyncMock(side_effect=_capture_ingest)), \
             patch.object(ra, "_generate_summary", AsyncMock(return_value="summary")), \
             patch.object(ra, "_update_session_iteration", AsyncMock()), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            events = []
            async for blob in ra.run_research("https://stackoverflow.com/a/12345", depth="medium"):
                events.append(blob)

        parsed = _parse_sse(events)
        etypes = [e for e, _ in parsed]

        # LLM extract was NOT called.
        tool_call_mock.assert_not_called()

        # distill_bypassed event emitted with the right classification.
        assert "distill_bypassed" in etypes
        bypass_evt = dict(parsed)["distill_bypassed"]
        assert bypass_evt["source_type"] == "so_answer"
        assert bypass_evt["url"] == "https://stackoverflow.com/a/12345"

        # Ingested entries carry source_type=so_answer + provenance.
        assert ingest_seen, "ingest_entries was never invoked"
        entries = ingest_seen[0]
        assert entries, "no entries produced via bypass path"
        assert all(e["source_type"] == "so_answer" for e in entries)
        assert all("provenance" in e for e in entries)

    @pytest.mark.asyncio
    async def test_url_mode_robots_blocked(self):
        """Robots disallow -> error event, no ingestion."""
        ingest_mock = AsyncMock()
        with patch.object(ra, "_guard_and_create_session", AsyncMock(return_value=(str(43), None))), \
             patch.object(ra, "_robots_allowed", AsyncMock(return_value=False)), \
             patch.object(ra, "ingest_entries", ingest_mock), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            events = []
            async for blob in ra.run_research("https://example.com/blocked", depth="medium"):
                events.append(blob)

            parsed = _parse_sse(events)
            etypes = [e for e, _ in parsed]
            assert "error" in etypes
            assert "research_complete" not in etypes
            ingest_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_url_mode_fetch_failed(self):
        """Fetch returns None -> error event."""
        with patch.object(ra, "_guard_and_create_session", AsyncMock(return_value=(str(44), None))), \
             patch.object(ra, "_robots_allowed", AsyncMock(return_value=True)), \
             patch.object(ra, "_fetch_url_bounded", AsyncMock(return_value=None)), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            events = []
            async for blob in ra.run_research("https://example.com/dead", depth="medium"):
                events.append(blob)

            parsed = _parse_sse(events)
            assert any(e == "error" for e, _ in parsed)

    @pytest.mark.asyncio
    async def test_topic_mode_unchanged(self):
        """Non-URL topic still takes the normal decompose/search path."""
        decompose_mock = AsyncMock(return_value={"facets": ["a"], "queries": [], "topic_complexity": "low"})
        with patch.object(ra, "_guard_and_create_session", AsyncMock(return_value=(str(45), None))), \
             patch.object(ra, "_decompose_topic", decompose_mock), \
             patch.object(ra, "_search_queries", AsyncMock(return_value=[])), \
             patch.object(ra, "_generate_summary", AsyncMock(return_value="s")), \
             patch.object(ra, "_update_session_iteration", AsyncMock()), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            events = []
            async for blob in ra.run_research("regular topic string", depth="shallow"):
                events.append(blob)

            decompose_mock.assert_called_once()
