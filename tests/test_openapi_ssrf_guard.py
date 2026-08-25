"""§17.812 (audit C1) — SSRF guard on the /research openapi: ingest path.

Before §17.812 the OpenAPI spec fetch (`app/utils/openapi_ingest._fetch_spec`)
called the shared HTTP client directly, bypassing the `_is_public_host` choke
point that guards every /research url: fetch. A token-holder (incl. a non-admin
scoped key under MULTI_USER_ENABLED — research isn't admin-gated) could issue
`/research openapi:http://169.254.169.254/…` or hit an internal Docker-network
service. Companion: prance resolves absolute-URL `$ref`s via its own fetcher,
outside the guard.

Coverage:
  - _fetch_spec rejects a loopback target before any network call
  - _fetch_spec re-validates and rejects a public→private redirect
  - _collect_absolute_ref_urls finds absolute http(s) refs, ignores internal
  - _resolve_refs skips resolution (returns unresolved) on a private-host $ref
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.openapi_ingest import (
    OpenAPIFetchError,
    _collect_absolute_ref_urls,
    _fetch_spec,
    _resolve_refs,
)

# §17.829 (plan 7.4) — smoke-marked so the cloud ci-smoke PR gate runs these
# (all mock-based, no network). openapi-spec-validator + prance were added to
# requirements-ci.txt for the module import; the old collect_ignore is gone.
pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# _fetch_spec — pre-fetch SSRF rejection (guard fires before the client call)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:5432/openapi.json",     # loopback (Postgres)
    "http://169.254.169.254/latest/meta-data/",  # link-local cloud metadata
    "http://10.0.0.5/spec.yaml",              # RFC1918
])
async def test_fetch_spec_rejects_private_target(url):
    """A private/loopback/link-local target is refused before any fetch."""
    # getaddrinfo on an IP literal is offline + deterministic — no network.
    with pytest.raises(OpenAPIFetchError) as exc:
        await _fetch_spec(url)
    assert "SSRF guard" in str(exc.value)


async def test_fetch_spec_rejects_non_http_scheme():
    with pytest.raises(OpenAPIFetchError) as exc:
        await _fetch_spec("file:///etc/passwd")
    assert "SSRF guard" in str(exc.value)


async def test_fetch_spec_rejects_public_to_private_redirect():
    """A public URL that 3xx-redirects to a private IP is caught on the
    post-redirect re-validation (the generic client follows redirects)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.url = "http://127.0.0.1/evil.json"   # redirected target (private)
    resp.text = "{}"
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    with patch("app.utils.http_clients.get_generic_http_client", return_value=client):
        with pytest.raises(OpenAPIFetchError) as exc:
            # initial host is a public IP literal → passes the pre-check
            await _fetch_spec("http://1.1.1.1/spec.json")
    assert "after redirect" in str(exc.value)
    assert "SSRF guard" in str(exc.value)


# ---------------------------------------------------------------------------
# _collect_absolute_ref_urls — the $ref scanner (pure, no deps)
# ---------------------------------------------------------------------------

def test_collect_finds_absolute_ignores_internal():
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/x": {"get": {"responses": {
                "200": {"$ref": "http://169.254.169.254/meta"},
            }}},
        },
        "components": {"schemas": {
            "Internal": {"$ref": "#/components/schemas/B"},         # internal
            "Rel": {"$ref": "defs.yaml#/X"},                        # relative
            "Remote": {"$ref": "https://public.example.com/d.json#/X"},
        }},
    }
    urls = _collect_absolute_ref_urls(spec)
    assert urls == {
        "http://169.254.169.254/meta",
        "https://public.example.com/d.json#/X",
    }


def test_collect_walks_lists_and_is_empty_when_none():
    assert _collect_absolute_ref_urls({"a": [{"b": {"$ref": "#/c"}}]}) == set()
    nested = {"allOf": [{"$ref": "http://evil.internal/x"}, {"type": "object"}]}
    assert _collect_absolute_ref_urls(nested) == {"http://evil.internal/x"}


# ---------------------------------------------------------------------------
# _resolve_refs — skip resolution on a private-host absolute $ref
# ---------------------------------------------------------------------------

async def test_resolve_refs_skips_on_private_ref():
    """A private-host absolute $ref makes _resolve_refs return the spec
    UNRESOLVED rather than let prance fetch the internal target."""
    spec = {
        "openapi": "3.0.0",
        "paths": {"/x": {"get": {"responses": {
            "200": {"$ref": "http://127.0.0.1/evil.json#/Thing"},
        }}}},
    }
    # Force the prance-available branch so we exercise the SSRF pre-check, not
    # the "prance missing" early return. ResolvingParser is never reached.
    with patch("app.utils.openapi_ingest._PRANCE_AVAILABLE", True):
        resolved, ok = await _resolve_refs(spec, "http://public.example.com/spec")
    assert ok is False
    assert resolved is spec
