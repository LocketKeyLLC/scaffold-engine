"""§17.1066 — the browser-fingerprint retry on 403/429."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import research_extractors as rx


class _Stream:
    def __init__(self, status): self.status_code = status; self.url = "https://x.example/p"; self.headers = {}; self.encoding = "utf-8"
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def aiter_bytes(self):
        yield b""


def _client(status):
    c = MagicMock(); c.stream = MagicMock(return_value=_Stream(status)); return c


@pytest.mark.asyncio
async def test_403_retries_once_with_impersonation_and_clears_the_failure():
    ra = MagicMock(); ra.get_generic_http_client.return_value = _client(403)
    failure = {}
    with patch.object(rx, "_ra", return_value=ra), \
         patch.object(rx, "_is_public_host", return_value=(True, "")), \
         patch.object(settings, "research_fetch_impersonate_enabled", True), \
         patch.object(rx, "_fetch_impersonated", new=AsyncMock(return_value="<html>recovered</html>")) as imp:
        out = await rx._fetch_url_bounded("https://x.example/p", failure=failure)
    assert out == "<html>recovered</html>" and failure == {}
    imp.assert_awaited_once()


@pytest.mark.asyncio
async def test_404_does_not_retry_and_keeps_the_reason():
    ra = MagicMock(); ra.get_generic_http_client.return_value = _client(404)
    failure = {}
    with patch.object(rx, "_ra", return_value=ra), \
         patch.object(rx, "_is_public_host", return_value=(True, "")), \
         patch.object(rx, "_fetch_impersonated", new=AsyncMock()) as imp:
        out = await rx._fetch_url_bounded("https://x.example/p", failure=failure)
    assert out is None and failure["reason"] == "http_404"
    imp.assert_not_awaited()


@pytest.mark.asyncio
async def test_impersonated_fetch_respects_cap_redirect_ssrf_and_status():
    resp = MagicMock(); resp.status_code = 200; resp.url = "https://x.example/p"; resp.content = b"a" * 10; resp.encoding = "utf-8"
    sess = MagicMock(); sess.__aenter__ = AsyncMock(return_value=sess); sess.__aexit__ = AsyncMock(return_value=False)
    sess.get = AsyncMock(return_value=resp)
    fake = MagicMock(); fake.requests.AsyncSession = MagicMock(return_value=sess)
    with patch.dict("sys.modules", {"curl_cffi": fake, "curl_cffi.requests": fake.requests}), \
         patch.object(rx, "_is_public_host", return_value=(True, "")):
        assert await rx._fetch_impersonated("https://x.example/p", cap=100, timeout=5) == "a" * 10
        assert await rx._fetch_impersonated("https://x.example/p", cap=5, timeout=5) is None      # over the cap
        resp.status_code = 403
        assert await rx._fetch_impersonated("https://x.example/p", cap=100, timeout=5) is None    # still blocked
        resp.status_code = 200; resp.url = "http://10.0.0.5/internal"
    with patch.dict("sys.modules", {"curl_cffi": fake, "curl_cffi.requests": fake.requests}), \
         patch.object(rx, "_is_public_host", return_value=(False, "private")):
        assert await rx._fetch_impersonated("https://x.example/p", cap=100, timeout=5) is None    # redirect to a private host
