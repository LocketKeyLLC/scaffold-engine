"""§17.93 SSRF guard — _is_public_host() and _fetch_url_bounded()
rejection paths.

Threat model: a token-holder POSTs /research with {"topic": "<URL>"}.
Pre-§17.93 the helper would GET any http(s) URL — internal services
(Ollama, Postgres, Milvus), cloud metadata, and other LAN hosts were
reachable from inside the orchestrator. §17.93 adds a private-IP /
loopback / link-local rejector at the fetch boundary; the rejection
covers URL-mode, OpenAPI-mode, and the PDF-mode (which doesn't fetch
URLs but uses the same helper for any future caller).

Coverage:
  - public-host happy paths (real DNS resolution against pre-known
    IP-literal cases so the test doesn't actually hit the network)
  - non-http schemes (file://, gopher://, ftp://)
  - literal private hostnames (localhost, 0.0.0.0, ip6-loopback)
  - private RFC1918 IPs (10/8, 172.16/12, 192.168/16)
  - loopback (127/8, ::1)
  - link-local (169.254/16, fe80::/10)
  - reserved / multicast / unspecified
  - DNS resolution failure (rejects, doesn't crash)
  - opt-out via settings.research_allow_private_hosts
  - end-to-end: _fetch_url_bounded returns None on rejected URL
"""
from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.research_extractors import _fetch_url_bounded, _is_public_host


def _fake_getaddrinfo(ip: str):
    """Build a getaddrinfo mock returning a single (fam, ..., (ip, port)) tuple."""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]


# ---------------------------------------------------------------------------
# Scheme rejection
# ---------------------------------------------------------------------------

class TestSchemeRejection:
    """Non-http(s) schemes are rejected without ever resolving DNS."""

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/pub/",
        "javascript:alert(1)",
        "data:text/html,<script>",
    ])
    def test_rejects_non_http_scheme(self, url):
        ok, reason = _is_public_host(url)
        assert ok is False
        assert "non_http_scheme" in reason


# ---------------------------------------------------------------------------
# Literal private hostnames (rejected without DNS)
# ---------------------------------------------------------------------------

class TestLiteralPrivateHostnames:
    @pytest.mark.parametrize("host", [
        "localhost", "LOCALHOST", "localhost.localdomain",
        "0.0.0.0", "ip6-localhost", "ip6-loopback",
    ])
    def test_rejects_literal_private(self, host):
        ok, reason = _is_public_host(f"http://{host}/")
        assert ok is False
        assert "literal_private_hostname" in reason

    def test_rejects_empty_ipv6_unspecified(self):
        """`http://::/` parses with empty hostname per urlparse — still
        rejected, just via the empty_hostname branch rather than the
        literal-private set. Both are explicit denies."""
        ok, reason = _is_public_host("http://::/")
        assert ok is False
        assert "empty_hostname" in reason or "literal_private_hostname" in reason


# ---------------------------------------------------------------------------
# IP-range rejection (via DNS mock)
# ---------------------------------------------------------------------------

class TestIpRangeRejection:
    """Hostnames that resolve to private/loopback/link-local/reserved/
    multicast/unspecified IPs are rejected."""

    @pytest.mark.parametrize("ip,name", [
        ("10.0.0.1", "rfc1918_10"),
        ("172.18.0.1", "rfc1918_172_16"),  # the bridge gateway (Ollama on host)
        ("192.168.1.1", "rfc1918_192_168"),
        ("127.0.0.1", "loopback_v4"),
        ("169.254.169.254", "link_local_v4_metadata"),
        ("::1", "loopback_v6"),
        ("fe80::1", "link_local_v6"),
        ("fc00::1", "ula_v6"),
        ("224.0.0.1", "multicast_v4"),
        ("0.0.0.0", "unspecified_v4"),
    ])
    def test_rejects_resolved_to_private_ip(self, ip, name):
        with patch(
            "app.modules.research_extractors.socket.getaddrinfo",
            return_value=_fake_getaddrinfo(ip),
        ):
            ok, reason = _is_public_host("http://attacker.example.com/")
        assert ok is False, f"{name}: expected reject but got accept"
        assert "resolved_to_private_ip" in reason
        assert ip in reason


# ---------------------------------------------------------------------------
# Public hosts (happy path)
# ---------------------------------------------------------------------------

class TestPublicHosts:
    @pytest.mark.parametrize("ip", [
        "8.8.8.8",        # public IPv4
        "1.1.1.1",        # public IPv4
        "208.80.154.224", # Wikipedia load balancer (approx)
        "2606:4700:4700::1111",  # Cloudflare public IPv6
    ])
    def test_accepts_public_ip(self, ip):
        with patch(
            "app.modules.research_extractors.socket.getaddrinfo",
            return_value=_fake_getaddrinfo(ip),
        ):
            ok, reason = _is_public_host("http://example.com/page")
        assert ok is True, f"{ip}: expected accept but got reject ({reason})"


# ---------------------------------------------------------------------------
# DNS failure
# ---------------------------------------------------------------------------

class TestDnsFailure:
    def test_rejects_on_dns_failure(self):
        """Unresolvable hostname rejects rather than crashing."""
        with patch(
            "app.modules.research_extractors.socket.getaddrinfo",
            side_effect=socket.gaierror("nodename nor servname provided"),
        ):
            ok, reason = _is_public_host("http://nonexistent.invalid/")
        assert ok is False
        assert "dns_resolve_failed" in reason


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------

class TestOptOut:
    def test_setting_opts_out_of_ip_check(self):
        """research_allow_private_hosts=True lets a private IP through.
        Used for local-development scenarios where the orchestrator
        legitimately fetches internal services."""
        from app.modules import research_extractors as rex
        with patch.object(rex.settings, "research_allow_private_hosts", True):
            ok, reason = _is_public_host("http://localhost/")
        assert ok is True
        assert reason == "private_hosts_allowed_by_setting"

    def test_opt_out_does_not_bypass_scheme_check(self):
        """Non-http schemes are rejected even with the opt-out flipped."""
        from app.modules import research_extractors as rex
        with patch.object(rex.settings, "research_allow_private_hosts", True):
            ok, reason = _is_public_host("file:///etc/passwd")
        assert ok is False
        assert "non_http_scheme" in reason


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
            "app.modules.research_extractors.socket.getaddrinfo",
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
