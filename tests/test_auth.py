"""Tests for app/auth.py (#9.19)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.fixture
def _api_key_set(monkeypatch):
    """Reload auth module with an API key set so tests are deterministic.

    Note: we monkeypatch `settings.scaffold_api_key` *in place* rather than
    `importlib.reload(app.config)`. Reloading config swaps in a new Settings
    singleton, but every other module that did `from app.config import
    settings` already holds a reference to the OLD instance — their
    `settings.X` reads then become invisible to subsequent test patches
    on the new instance. That cross-test leak surfaced as flaky failures
    in test_dag_generator and test_execution_agent_compile when this
    fixture ran earlier in the suite. Reloading only `app.auth` (which
    captures `_RAW_KEY` at import time) is sufficient and isolation-safe.

    Settings revert is done MANUALLY (not via monkeypatch.setattr) so we
    control ordering: revert settings FIRST, then reload app.auth so its
    re-captured ``_RAW_KEY`` matches the restored settings value.
    monkeypatch.setattr reverts AFTER the fixture's yield-body runs,
    which would leave _RAW_KEY stuck at the patched ``testkey123`` and
    break subsequent endpoint tests in the same suite run.
    """
    import importlib
    from pydantic import SecretStr
    import app.config
    import app.auth
    monkeypatch.setenv("SCAFFOLD_API_KEY", "testkey123")
    original_key = app.config.settings.scaffold_api_key
    app.config.settings.scaffold_api_key = SecretStr("testkey123")
    importlib.reload(app.auth)
    yield app.auth
    # Order matters here — restore settings FIRST, reload auth SECOND.
    app.config.settings.scaffold_api_key = original_key
    importlib.reload(app.auth)


@pytest.fixture
def _api_key_unset(monkeypatch):
    """Reload auth with empty key AND SCAFFOLD_AUTH_DISABLED=1 (the real dev opt-out).

    Empty key WITHOUT SCAFFOLD_AUTH_DISABLED=1 raises RuntimeError at module
    import — that's the documented contract in app/auth.py:11-15. The
    "permissive" mode is only reachable via the explicit opt-out flag.

    Same manual-restore pattern as _api_key_set — see that fixture for
    why monkeypatch.setattr would leave _RAW_KEY stale across tests.
    """
    import importlib
    from pydantic import SecretStr
    import app.config
    import app.auth
    monkeypatch.delenv("SCAFFOLD_API_KEY", raising=False)
    monkeypatch.setenv("SCAFFOLD_AUTH_DISABLED", "1")
    original_key = app.config.settings.scaffold_api_key
    original_disabled = app.config.settings.scaffold_auth_disabled
    app.config.settings.scaffold_api_key = SecretStr("")
    app.config.settings.scaffold_auth_disabled = True
    importlib.reload(app.auth)
    yield app.auth
    app.config.settings.scaffold_api_key = original_key
    app.config.settings.scaffold_auth_disabled = original_disabled
    importlib.reload(app.auth)


def _mk_request(path: str) -> MagicMock:
    req = MagicMock()
    req.url = MagicMock()
    req.url.path = path
    return req


@pytest.mark.smoke
async def test_health_path_passes_without_key(_api_key_set):
    """/health bypasses auth even when a key is set."""
    result = await _api_key_set.require_api_key(_mk_request("/health"), key=None)
    assert result == ""


@pytest.mark.smoke
async def test_valid_key_is_accepted(_api_key_set):
    result = await _api_key_set.require_api_key(_mk_request("/dag/abc"), key="testkey123")
    assert result == "testkey123"


@pytest.mark.smoke
async def test_missing_key_raises_401(_api_key_set):
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_api_key(_mk_request("/dag/abc"), key=None)
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_wrong_key_raises_401(_api_key_set):
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_api_key(_mk_request("/dag/abc"), key="nope")
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_non_ascii_key_raises_401_not_typeerror(_api_key_set):
    """§17.596 — a non-ASCII X-API-Key (latin-1 header decode) must yield a
    clean 401, not a `TypeError: comparing strings with non-ASCII characters`
    from secrets.compare_digest that escapes to a 500 + error_logs row."""
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_api_key(_mk_request("/dag/abc"), key="café\xff")
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_explicit_auth_disabled_returns_empty(_api_key_unset):
    """SCAFFOLD_AUTH_DISABLED=1 with no key set bypasses auth (the dev opt-out).

    This is the only way to run without auth. Empty key alone raises
    RuntimeError at app.auth import time — see app/auth.py:11-15.
    """
    result = await _api_key_unset.require_api_key(_mk_request("/dag/abc"), key=None)
    assert result == ""


# ===========================================================================
# §17.266 — _AUTH_EXEMPT_PREFIXES regression tests (test-gap from §17.258)
# ===========================================================================
#
# The /web/ + /static/ prefix bypass is security-sensitive: any future
# change that adds /admin/* or drops a trailing slash would silently
# expose authenticated endpoints. Pre-§17.266 the prefix logic at
# auth.py:54 had no test coverage — only the exact-path /health bypass
# was guarded.


@pytest.mark.smoke
@pytest.mark.parametrize("path", [
    "/web/",                # bare prefix
    "/web/index.html",      # one segment
    "/web/static/css/main.css",  # nested
    "/static/",             # bare prefix
    "/static/css/app.css",  # nested
    "/static/js/bundle.min.js",
    "/ui/",                 # §17.778 — standalone operator SPA (bare prefix)
    "/ui/index.html",       # SPA entry
    "/ui/static/app.js",    # SPA asset (nested)
    "/ui/static/views/dag.js",
])
async def test_exempt_prefix_paths_bypass_without_key(_api_key_set, path):
    """Every path under /web/ and /static/ must bypass auth WITHOUT a key.
    The native web UI and its CSS load from a browser that doesn't carry
    the X-API-Key header — embedded SDK Client supplies the key on the
    loopback HTTP call to the real endpoints."""
    result = await _api_key_set.require_api_key(_mk_request(path), key=None)
    assert result == "", f"path {path!r} must bypass auth without a key"


@pytest.mark.smoke
@pytest.mark.parametrize("path", [
    # §17.266 — prefix-confusable paths that must NOT bypass. If someone
    # ever changes _AUTH_EXEMPT_PREFIXES to ("/web", "/static") (dropping
    # the trailing slash), every entry below would slip through.
    "/webhook",                  # starts with /web but is a real endpoint
    "/webhooks/incoming",        # /webhooks plural
    "/staticfile",               # starts with /static but is a real endpoint
    "/statics",                  # /statics plural (hypothetical)
    "/admin/web/",               # /web appears mid-path
    "/admin/static/",            # /static appears mid-path
    "/api/v1/web",               # /web as a final segment, no trailing slash
])
async def test_prefix_confusable_paths_require_key(_api_key_set, path):
    """Paths that LOOK like they might bypass but don't. Guards against
    a future maintainer dropping the trailing slash on the exempt prefix
    tuple — which would silently exempt /webhook, /staticfile, etc."""
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_api_key(_mk_request(path), key=None)
    assert exc_info.value.status_code == 401, (
        f"path {path!r} must require auth (would slip through if "
        f"_AUTH_EXEMPT_PREFIXES dropped trailing slashes)"
    )


@pytest.mark.smoke
async def test_exempt_prefix_does_not_validate_key_when_present(_api_key_set):
    """A request that hits an exempt prefix WITH a (wrong) key still
    bypasses — the prefix check fires before key validation. Locks in
    that the exempt prefixes are unconditional, not "exempt-if-no-key"."""
    result = await _api_key_set.require_api_key(
        _mk_request("/web/index.html"), key="totally-wrong-key",
    )
    assert result == "", "exempt prefix must bypass even when a wrong key is supplied"


# ===========================================================================
# §17.788 — require_openai_key: the native /v1 surface accepts a Bearer token
# (what OpenAI clients send) OR X-API-Key, against the same SCAFFOLD_API_KEY.
# ===========================================================================


@pytest.mark.smoke
async def test_openai_key_accepts_bearer(_api_key_set):
    result = await _api_key_set.require_openai_key(bearer="Bearer testkey123", x_api_key=None)
    assert result == "testkey123"


@pytest.mark.smoke
async def test_openai_key_accepts_bearer_case_insensitive(_api_key_set):
    result = await _api_key_set.require_openai_key(bearer="bearer testkey123", x_api_key=None)
    assert result == "testkey123"


@pytest.mark.smoke
async def test_openai_key_accepts_bare_token(_api_key_set):
    """Tolerant of a bare token with no ``Bearer `` prefix."""
    result = await _api_key_set.require_openai_key(bearer="testkey123", x_api_key=None)
    assert result == "testkey123"


@pytest.mark.smoke
async def test_openai_key_accepts_x_api_key_fallback(_api_key_set):
    """The /ui SPA sends X-API-Key, not Bearer — it must still authenticate."""
    result = await _api_key_set.require_openai_key(bearer=None, x_api_key="testkey123")
    assert result == "testkey123"


@pytest.mark.smoke
async def test_openai_key_bearer_precedence_over_x_api_key(_api_key_set):
    """When both are present, a valid Bearer authenticates even if X-API-Key is junk."""
    result = await _api_key_set.require_openai_key(bearer="Bearer testkey123", x_api_key="junk")
    assert result == "testkey123"


@pytest.mark.smoke
async def test_openai_key_rejects_wrong(_api_key_set):
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_openai_key(bearer="Bearer nope", x_api_key=None)
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_openai_key_rejects_missing(_api_key_set):
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_openai_key(bearer=None, x_api_key=None)
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_openai_key_non_ascii_raises_401_not_typeerror(_api_key_set):
    """§17.596 — non-ASCII header bytes must yield a clean 401, not a TypeError."""
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_openai_key(bearer="Bearer caf\xe9\xff", x_api_key=None)
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_openai_key_auth_disabled_bypasses(_api_key_unset):
    """SCAFFOLD_AUTH_DISABLED=1 bypasses the /v1 guard too (dev opt-out parity)."""
    result = await _api_key_unset.require_openai_key(bearer=None, x_api_key=None)
    assert result == ""


@pytest.mark.smoke
async def test_exempt_prefixes_set_shape_is_loadable(_api_key_set):
    """Sanity: the constant is iterable as a tuple of strings, every
    entry ends with '/'. Guards against typos like ``"/web"`` (missing
    slash) sneaking in."""
    prefixes = _api_key_set._AUTH_EXEMPT_PREFIXES
    assert isinstance(prefixes, tuple), f"expected tuple, got {type(prefixes)}"
    for p in prefixes:
        assert isinstance(p, str), f"prefix {p!r} is not a string"
        assert p.endswith("/"), (
            f"prefix {p!r} missing trailing '/' — would match too broadly "
            f"(e.g. /web matches /webhook). See §17.266."
        )
