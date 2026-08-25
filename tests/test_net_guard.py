"""§17.829 (plan 7.4) — _is_public_host() SSRF guard tests, smoke tier.

These are the guard-function tests that lived in test_research_ssrf_guard.py
(§17.93). They moved here when the guard moved to ``app/utils/net_guard.py``:
that module needs only stdlib + app.config, so this file runs in the cloud
ci-smoke PR gate — pre-§17.829 the fast gate had NO SSRF coverage (audit M11)
because research_extractors' pdfplumber/trafilatura imports made the old test
module uncollectable there.

The fetch-boundary tests (``_fetch_url_bounded`` short-circuit, topic-fetch
routing) stay in test_research_ssrf_guard.py — they genuinely need the heavy
module and keep running in `make test` / the CI unit-tests job.

Threat model (unchanged, §17.93): a token-holder POSTs /research with a URL
that resolves to internal services (Ollama, Postgres, Milvus), cloud
metadata, or other LAN hosts. The guard rejects at the fetch boundary.
"""
from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from app.utils.net_guard import _is_public_host

pytestmark = pytest.mark.smoke


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
            "app.utils.net_guard.socket.getaddrinfo",
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
            "app.utils.net_guard.socket.getaddrinfo",
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
            "app.utils.net_guard.socket.getaddrinfo",
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
        from app.utils import net_guard
        with patch.object(net_guard.settings, "research_allow_private_hosts", True):
            ok, reason = _is_public_host("http://localhost/")
        assert ok is True
        assert reason == "private_hosts_allowed_by_setting"

    def test_opt_out_does_not_bypass_scheme_check(self):
        """Non-http schemes are rejected even with the opt-out flipped."""
        from app.utils import net_guard
        with patch.object(net_guard.settings, "research_allow_private_hosts", True):
            ok, reason = _is_public_host("file:///etc/passwd")
        assert ok is False
        assert "non_http_scheme" in reason
