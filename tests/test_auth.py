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
    """
    import importlib
    from pydantic import SecretStr
    import app.config
    import app.auth
    monkeypatch.setenv("SCAFFOLD_API_KEY", "testkey123")
    monkeypatch.setattr(
        app.config.settings, "scaffold_api_key", SecretStr("testkey123"),
    )
    importlib.reload(app.auth)
    yield app.auth
    # Teardown: monkeypatch restores settings.scaffold_api_key + env, but
    # auth.py captured the OLD _RAW_KEY at the start of this fixture. Reload
    # auth one more time so subsequent tests see a key consistent with the
    # restored settings.scaffold_api_key.
    importlib.reload(app.auth)


@pytest.fixture
def _api_key_unset(monkeypatch):
    """Reload auth with empty key AND SCAFFOLD_AUTH_DISABLED=1 (the real dev opt-out).

    Empty key WITHOUT SCAFFOLD_AUTH_DISABLED=1 raises RuntimeError at module
    import — that's the documented contract in app/auth.py:11-15. The
    "permissive" mode is only reachable via the explicit opt-out flag.

    Same monkeypatch-in-place strategy as _api_key_set; see that fixture
    for why we don't reload app.config.
    """
    import importlib
    from pydantic import SecretStr
    import app.config
    import app.auth
    monkeypatch.delenv("SCAFFOLD_API_KEY", raising=False)
    monkeypatch.setenv("SCAFFOLD_AUTH_DISABLED", "1")
    monkeypatch.setattr(app.config.settings, "scaffold_api_key", SecretStr(""))
    monkeypatch.setattr(app.config.settings, "scaffold_auth_disabled", True)
    importlib.reload(app.auth)
    yield app.auth
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
async def test_explicit_auth_disabled_returns_empty(_api_key_unset):
    """SCAFFOLD_AUTH_DISABLED=1 with no key set bypasses auth (the dev opt-out).

    This is the only way to run without auth. Empty key alone raises
    RuntimeError at app.auth import time — see app/auth.py:11-15.
    """
    result = await _api_key_unset.require_api_key(_mk_request("/dag/abc"), key=None)
    assert result == ""
