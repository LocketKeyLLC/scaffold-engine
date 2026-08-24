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
async def test_exempt_prefix_paths_bypass_without_key(_api_key_set, path, monkeypatch):
    """Every path under /web/, /static/, /ui/ must bypass auth WITHOUT a key.
    The native web UI and its CSS load from a browser that doesn't carry
    the X-API-Key header — embedded SDK Client supplies the key on the
    loopback HTTP call to the real endpoints.

    §17.812 — /web exemption is single-user-only, so pin the mode explicitly
    (the ambient container may run MULTI_USER_ENABLED=true)."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", False)
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
async def test_exempt_prefix_does_not_validate_key_when_present(_api_key_set, monkeypatch):
    """A request that hits an exempt prefix WITH a (wrong) key still
    bypasses — the prefix check fires before key validation. Locks in
    that the exempt prefixes are unconditional, not "exempt-if-no-key".

    §17.812 — /web is single-user-exempt, so pin single-user mode."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", False)
    result = await _api_key_set.require_api_key(
        _mk_request("/web/index.html"), key="totally-wrong-key",
    )
    assert result == "", "exempt prefix must bypass even when a wrong key is supplied"


# ===========================================================================
# §17.807 — multi-user scoped keys (MULTI_USER_ENABLED)
# ===========================================================================
#
# When multi_user_enabled is True, auth accepts the master key (admin) OR any
# live scoped key from the api_keys table, matched by SHA-256 digest. The DB is
# consulted ONLY for a non-master key, and ONLY when the mode is on — single-
# user installs keep the pure in-memory compare with no per-request query.


def _fake_session_factory():
    """A stand-in for app.database.async_session — an async-context-manager
    factory that yields a throwaway session object (never actually queried,
    because verify_key is mocked)."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock())
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


@pytest.mark.smoke
async def test_multiuser_master_key_still_admin(_api_key_set, monkeypatch):
    """Master key authenticates as admin in multi-user mode WITHOUT a DB hit,
    and attaches the admin Principal to request.state (§17.810)."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", True)
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(_api_key_set, "resolve_key", resolve)
    monkeypatch.setattr(_api_key_set, "async_session", _fake_session_factory())

    req = _mk_request("/dag/abc")
    result = await _api_key_set.require_api_key(req, key="testkey123")
    assert result == "testkey123"
    resolve.assert_not_awaited()  # admin path short-circuits before the DB
    assert req.state.principal.is_admin  # §17.810 — admin principal attached


@pytest.mark.smoke
async def test_multiuser_live_scoped_key_accepted(_api_key_set, monkeypatch):
    """A non-master key that resolve_key confirms as live is accepted, and its
    Principal (identity from owner tag, role from row) is attached (§17.810)."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", True)
    resolve = AsyncMock(return_value={"id": 7, "owner": "alice", "role": "user"})
    monkeypatch.setattr(_api_key_set, "resolve_key", resolve)
    monkeypatch.setattr(_api_key_set, "async_session", _fake_session_factory())

    req = _mk_request("/dag/abc")
    result = await _api_key_set.require_api_key(req, key="sk-scaffold-somelivekey")
    assert result == "sk-scaffold-somelivekey"
    resolve.assert_awaited_once()
    assert req.state.principal.identity == "alice"
    assert req.state.principal.role == "user"
    assert not req.state.principal.is_admin


@pytest.mark.smoke
async def test_multiuser_revoked_or_unknown_key_401(_api_key_set, monkeypatch):
    """A non-master key that resolve_key rejects (revoked/unknown) → 401."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", True)
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(_api_key_set, "resolve_key", resolve)
    monkeypatch.setattr(_api_key_set, "async_session", _fake_session_factory())

    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_api_key(
            _mk_request("/dag/abc"), key="sk-scaffold-revoked",
        )
    assert exc_info.value.status_code == 401
    resolve.assert_awaited_once()


@pytest.mark.smoke
async def test_singleuser_does_not_consult_db(_api_key_set, monkeypatch):
    """With multi_user_enabled False (default), a non-master key is rejected
    WITHOUT ever touching the DB — the scoped-key lookup is gated on the mode."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", False)
    resolve = AsyncMock(return_value={"id": 1, "owner": "x", "role": "user"})  # must not be consulted
    monkeypatch.setattr(_api_key_set, "resolve_key", resolve)
    monkeypatch.setattr(_api_key_set, "async_session", _fake_session_factory())

    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_api_key(_mk_request("/dag/abc"), key="sk-scaffold-live")
    assert exc_info.value.status_code == 401
    resolve.assert_not_awaited()
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


# ===========================================================================
# §17.812 (audit C9) — /web is gated under MULTI_USER_ENABLED
# ===========================================================================
#
# /web is server-rendered admin-view HTML whose loopback re-auths as master;
# authz resolves the exempt path to ADMIN. Single-user: harmless (sole user).
# Multi-user: it would expose every user's jobs to an unauthenticated browser,
# so the /web exemption is withdrawn in that mode (browsers get 401 → use /ui).


@pytest.mark.smoke
async def test_web_prefix_still_exempt_single_user(_api_key_set, monkeypatch):
    """Single-user: /web stays exempt — no regression from §17.266."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", False)
    result = await _api_key_set.require_api_key(_mk_request("/web/index.html"), key=None)
    assert result == "", "/web must stay exempt in single-user mode"


@pytest.mark.smoke
async def test_web_prefix_gated_under_multi_user(_api_key_set, monkeypatch):
    """Multi-user: an unauthenticated /web request no longer bypasses — it
    falls through to key validation and 401s."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", True)
    monkeypatch.setattr(_api_key_set, "resolve_key", AsyncMock(return_value=None))
    monkeypatch.setattr(_api_key_set, "async_session", _fake_session_factory())
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_api_key(_mk_request("/web/index.html"), key=None)
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_web_prefix_multi_user_master_key_ok(_api_key_set, monkeypatch):
    """Multi-user: the master key still reaches /web (admin)."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", True)
    monkeypatch.setattr(_api_key_set, "resolve_key", AsyncMock(return_value=None))
    monkeypatch.setattr(_api_key_set, "async_session", _fake_session_factory())
    req = _mk_request("/web/index.html")
    result = await _api_key_set.require_api_key(req, key="testkey123")
    assert result == "testkey123"


@pytest.mark.smoke
async def test_ui_and_static_stay_exempt_under_multi_user(_api_key_set, monkeypatch):
    """The /ui SPA + /static assets stay exempt even in multi-user mode — the
    SPA sends its own X-API-Key on API calls; only /web is withdrawn."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", True)
    for path in ("/ui/index.html", "/ui/static/app.js", "/static/css/app.css"):
        result = await _api_key_set.require_api_key(_mk_request(path), key=None)
        assert result == "", f"{path} must stay exempt in multi-user mode"


# ===========================================================================
# §17.812 (audit LOW) — require_openai_key empty-master-key symmetry
# ===========================================================================
#
# The /v1 surface's bearer check lacked the bool(_RAW_KEY) guard that the
# master path in require_api_key has. Under MULTI_USER_ENABLED the master key
# may be empty; without the guard an empty candidate would compare "" == "" and
# authenticate. _bearer_token returns None for empty input today, but a direct
# empty X-API-Key ("") reaches compare_digest as "" — this locks the guard in.


@pytest.fixture
def _empty_master_multiuser(monkeypatch):
    """Empty master key, MULTI_USER_ENABLED on, auth NOT disabled — the one
    configuration where require_openai_key could compare "" == "" (allowed by
    the import-time guard, app/auth.py:20, only because multi-user is on)."""
    import importlib
    from pydantic import SecretStr
    import app.config
    import app.auth
    original_key = app.config.settings.scaffold_api_key
    original_mu = app.config.settings.multi_user_enabled
    original_disabled = app.config.settings.scaffold_auth_disabled
    app.config.settings.scaffold_api_key = SecretStr("")
    app.config.settings.multi_user_enabled = True
    app.config.settings.scaffold_auth_disabled = False
    importlib.reload(app.auth)
    yield app.auth
    app.config.settings.scaffold_api_key = original_key
    app.config.settings.multi_user_enabled = original_mu
    app.config.settings.scaffold_auth_disabled = original_disabled
    importlib.reload(app.auth)


@pytest.mark.smoke
async def test_openai_key_empty_master_rejects_empty_x_api_key(_empty_master_multiuser):
    """The regression: empty X-API-Key against an empty master must 401, not
    slip through compare_digest("", "")."""
    with pytest.raises(HTTPException) as exc_info:
        await _empty_master_multiuser.require_openai_key(bearer=None, x_api_key="")
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_openai_key_empty_master_rejects_empty_bearer(_empty_master_multiuser):
    """Empty bearer token against an empty master must 401."""
    with pytest.raises(HTTPException) as exc_info:
        await _empty_master_multiuser.require_openai_key(bearer="Bearer ", x_api_key=None)
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_openai_key_valid_master_accepted(_api_key_set):
    """Sanity: a real master key is still accepted via both header forms."""
    assert await _api_key_set.require_openai_key(
        bearer="Bearer testkey123", x_api_key=None) == "testkey123"
    assert await _api_key_set.require_openai_key(
        bearer=None, x_api_key="testkey123") == "testkey123"


@pytest.mark.smoke
async def test_openai_key_wrong_key_rejected(_api_key_set):
    """A wrong key still 401s (guard didn't loosen the normal path)."""
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_openai_key(bearer="Bearer nope", x_api_key=None)
    assert exc_info.value.status_code == 401
