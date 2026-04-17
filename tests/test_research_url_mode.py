"""Behavioral tests for /research <url> URL-mode branch."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import research_agent as ra


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
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            assert await ra._robots_allowed("https://example.com/page") is True

    @pytest.mark.asyncio
    async def test_allowed_when_exception(self):
        with patch("httpx.AsyncClient", side_effect=Exception("network down")):
            assert await ra._robots_allowed("https://example.com/page") is True

    @pytest.mark.asyncio
    async def test_disallowed_by_robots(self):
        robots_txt = "User-agent: *\nDisallow: /private/"
        mock_resp = MagicMock(status_code=200, text=robots_txt)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            allowed = await ra._robots_allowed("https://example.com/private/secret")
            assert allowed is False

    @pytest.mark.asyncio
    async def test_allowed_by_robots(self):
        robots_txt = "User-agent: *\nDisallow: /private/"
        mock_resp = MagicMock(status_code=200, text=robots_txt)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
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

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_resp
        mock_client_cm = AsyncMock()
        mock_client_cm.__aenter__.return_value.stream = MagicMock(return_value=mock_stream_cm)

        with patch("httpx.AsyncClient", return_value=mock_client_cm):
            result = await ra._fetch_url_bounded("https://example.com/big")
            assert result is None

    @pytest.mark.asyncio
    async def test_fetches_small_page(self):
        body = b"<html><body>hello world</body></html>"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": str(len(body))}
        mock_resp.encoding = "utf-8"

        async def _chunks():
            yield body
        mock_resp.aiter_bytes = _chunks

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_resp
        mock_client_cm = AsyncMock()
        mock_client_cm.__aenter__.return_value.stream = MagicMock(return_value=mock_stream_cm)

        with patch("httpx.AsyncClient", return_value=mock_client_cm):
            result = await ra._fetch_url_bounded("https://example.com/small")
            assert result == "<html><body>hello world</body></html>"

    @pytest.mark.asyncio
    async def test_rejects_non_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.headers = {}

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_resp
        mock_client_cm = AsyncMock()
        mock_client_cm.__aenter__.return_value.stream = MagicMock(return_value=mock_stream_cm)

        with patch("httpx.AsyncClient", return_value=mock_client_cm):
            result = await ra._fetch_url_bounded("https://example.com/err")
            assert result is None

    @pytest.mark.asyncio
    async def test_mid_stream_cap(self):
        big_chunk = b"x" * (6 * 1024 * 1024)  # 6 MB, one chunk
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}  # no content-length
        mock_resp.encoding = "utf-8"

        async def _chunks():
            yield big_chunk
        mock_resp.aiter_bytes = _chunks

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_resp
        mock_client_cm = AsyncMock()
        mock_client_cm.__aenter__.return_value.stream = MagicMock(return_value=mock_stream_cm)

        with patch("httpx.AsyncClient", return_value=mock_client_cm):
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
        fake_resp = MagicMock(success=True, text='[{"title":"T","content":"body content here long enough","tags":"","source":"https://example.com/page","source_type":"community"}]', error=None)

        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=None)), \
             patch.object(ra, "_create_session", AsyncMock(return_value=42)), \
             patch.object(ra, "_robots_allowed", AsyncMock(return_value=True)), \
             patch.object(ra, "_fetch_url_bounded", AsyncMock(return_value="<html>x</html>")), \
             patch("asyncio.to_thread", AsyncMock(return_value="Clean article body " * 30)), \
             patch.object(ra.model_router, "generate", AsyncMock(return_value=fake_resp)), \
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
    async def test_url_mode_robots_blocked(self):
        """Robots disallow -> error event, no ingestion."""
        ingest_mock = AsyncMock()
        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=None)), \
             patch.object(ra, "_create_session", AsyncMock(return_value=43)), \
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
        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=None)), \
             patch.object(ra, "_create_session", AsyncMock(return_value=44)), \
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
        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=None)), \
             patch.object(ra, "_create_session", AsyncMock(return_value=45)), \
             patch.object(ra, "_decompose_topic", decompose_mock), \
             patch.object(ra, "_search_queries", AsyncMock(return_value=[])), \
             patch.object(ra, "_generate_summary", AsyncMock(return_value="s")), \
             patch.object(ra, "_update_session_iteration", AsyncMock()), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            events = []
            async for blob in ra.run_research("regular topic string", depth="shallow"):
                events.append(blob)

            decompose_mock.assert_called_once()
