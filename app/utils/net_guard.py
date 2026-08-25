"""§17.93 SSRF guard, extracted to a dependency-light module (§17.829).

``_is_public_host`` began life in ``app/modules/research_extractors.py``
(§17.93) and is the choke point for every outbound-URL fetch the engine makes
on a token-holder's behalf: ``/research url:`` (``_fetch_url_bounded``),
``/research openapi:`` (``app/utils/openapi_ingest``), and any future caller.

It lives here (plan 7.4, audit M11 "fast PR gate has no SSRF test") because
``research_extractors`` imports pdfplumber/trafilatura/pypdf — none of which
exist in the lightweight cloud ci-smoke env — which kept every SSRF guard
test out of the fast PR gate. This module needs only stdlib + ``app.config``,
so ``tests/test_net_guard.py`` runs on every PR.

``research_extractors`` re-exports ``_is_public_host`` / ``_PRIVATE_HOSTNAMES``
for backward compatibility (existing imports and patch targets keep working).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.config import settings

_PRIVATE_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain",
    "0.0.0.0", "::", "ip6-localhost", "ip6-loopback",
})


def _is_public_host(url: str) -> tuple[bool, str]:
    """§17.93 SSRF guard — return (ok, reason) for a target URL.

    Rejects:
      - non-http(s) schemes (file://, gopher://, etc.)
      - literal private hostnames (localhost, 0.0.0.0, ip6-loopback)
      - hostnames that resolve to any IPv4/IPv6 address in:
        loopback, link-local, private (RFC1918, ULA), unspecified,
        reserved, or multicast space.

    ``settings.research_allow_private_hosts`` (default False) opts
    out for local-development scenarios. The opt-out applies to the
    full resolution check, not the scheme check — non-HTTP schemes
    are always rejected.
    """
    try:
        p = urlparse(url.strip())
    except Exception as e:
        return False, f"url_parse_failed: {e}"
    if p.scheme not in ("http", "https"):
        return False, f"non_http_scheme: {p.scheme!r}"
    host = (p.hostname or "").lower().strip()
    if not host:
        return False, "empty_hostname"
    if settings.research_allow_private_hosts:
        return True, "private_hosts_allowed_by_setting"
    if host in _PRIVATE_HOSTNAMES:
        return False, f"literal_private_hostname: {host!r}"
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return False, f"dns_resolve_failed: {e}"
    for fam, _stype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"unparseable_resolved_ip: {ip_str!r}"
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return False, (
                f"resolved_to_private_ip: host={host!r} ip={ip_str!r} "
                f"flags=private:{ip.is_private},loopback:{ip.is_loopback},"
                f"link_local:{ip.is_link_local}"
            )
    return True, "public_host"
