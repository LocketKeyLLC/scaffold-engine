"""§17.93 SSRF guard — fetch-boundary rejection paths.

Threat model: a token-holder POSTs /research with {"topic": "<URL>"}.
Pre-§17.93 the helper would GET any http(s) URL — internal services
(Ollama, Postgres, Milvus), cloud metadata, and other LAN hosts were
reachable from inside the orchestrator. §17.93 adds a private-IP /
loopback / link-local rejector at the fetch boundary; the rejection
covers URL-mode, OpenAPI-mode, and the PDF-mode (which doesn't fetch
URLs but uses the same helper for any future caller).

§17.829 (plan 7.4): the guard-FUNCTION tests (schemes, literal private
hostnames, IP ranges, DNS failure, opt-out) moved to tests/test_net_guard.py
when ``_is_public_host`` moved to the dependency-light
``app/utils/net_guard.py`` — they now run in the cloud ci-smoke PR gate.
This module keeps the tests that need the heavy research modules
(pdfplumber/trafilatura imports; conftest collect_ignores it in smoke mode):

  - end-to-end: _fetch_url_bounded returns None on rejected URL, no HTTP
    call attempted
  - §17.612: topic-mode fetch routes through the hardened helper
"""
from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.research_extractors import _fetch_url_bounded


def _fake_getaddrinfo(ip: str):
    """Build a getaddrinfo mock returning a single (fam, ..., (ip, port)) tuple."""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]


# ---------------------------------------------------------------------------
# End-to-end: _fetch_url_bounded short-circuits on rejected URL
# ---------------------------------------------------------------------------

class TestFetchShortCircuit:
    @pytest.mark.asyncio
    async def test_fetch_returns_none_for_private_target(self):
        """The fetch helper rejects pre-network so no HTTP call is made."""
        client_mock = AsyncMock()
        client_mock.stream = AsyncMock()  # would fail if called
        with patch(
            "app.modules.research_extractors._ra"
        ) as ra_mock, patch(
            # §17.829 — the guard (and its socket reference) lives in net_guard.
            "app.utils.net_guard.socket.getaddrinfo",
            return_value=_fake_getaddrinfo("172.18.0.1"),
        ):
            ra_mock.return_value.get_generic_http_client.return_value = client_mock
            result = await _fetch_url_bounded("http://attacker.example.com/")
        assert result is None
        # No HTTP call attempted.
        client_mock.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_returns_none_for_literal_localhost(self):
        client_mock = AsyncMock()
        client_mock.stream = AsyncMock()
        with patch("app.modules.research_extractors._ra") as ra_mock:
            ra_mock.return_value.get_generic_http_client.return_value = client_mock
            result = await _fetch_url_bounded("http://localhost:8000/health")
        assert result is None
        client_mock.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_robots_allowed_never_fetches_private_target(self):
        """§17.854 (audit D2) — _robots_allowed must run the SSRF guard BEFORE
        issuing the robots.txt GET; a private/link-local host is fail-open with
        no HTTP call (URL mode calls this before the guarded page fetch)."""
        from app.modules.research_extractors import _robots_allowed
        client_mock = AsyncMock()
        client_mock.get = AsyncMock()  # would fail the assert if called
        with patch("app.modules.research_extractors._ra") as ra_mock:
            ra_mock.return_value.get_generic_http_client.return_value = client_mock
            allowed = await _robots_allowed("http://169.254.169.254/latest/meta-data")
        assert allowed is True  # fail-open
        client_mock.get.assert_not_called()


# ---------------------------------------------------------------------------
# §17.612 (audit #6) — topic-mode fetch routes through the hardened helper
# ---------------------------------------------------------------------------
class TestTopicFetchRoutesThroughGuard:
    """_fetch_and_extract must fetch via _fetch_url_bounded (SSRF pre/post-redirect
    check + byte cap), NOT a raw client.get that could buffer an unbounded body
    and follow a redirect to a private IP."""

    @pytest.mark.asyncio
    async def test_uses_fetch_url_bounded(self):
        from app.modules import research_agent
        calls = []

        async def fake_bounded(url, *a, **k):
            calls.append(url)
            # Simulate the guard rejecting one URL and allowing the other.
            return None if "private" in url else "x" * 500

        with patch.object(research_agent, "_fetch_url_bounded", side_effect=fake_bounded), \
             patch.object(research_agent.trafilatura, "extract", return_value="y" * 200):
            out = await research_agent._fetch_and_extract([
                {"url": "http://public.example.com/a"},
                {"url": "http://private.internal/b"},
            ])

        # Both URLs went through the guarded helper...
        assert set(calls) == {"http://public.example.com/a", "http://private.internal/b"}
        # ...and only the allowed one produced content (the rejected one → None → dropped).
        assert [o["url"] for o in out] == ["http://public.example.com/a"]
