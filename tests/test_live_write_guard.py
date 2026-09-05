"""§17.934 — the guard that stops the suite writing into the operator's engine.

Background: `make test` runs INSIDE the orchestrator container, which exports
SCAFFOLD_API_KEY and resolves scaffold-orchestrator:8000 to itself. Any test
that escaped its mocks authenticated as the MASTER key against the real
database. The `test_scaffold_router_*` lane did exactly that and injected 61
fixture turns into the operator's live assist session.
"""
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from tests import _live_write_guard
from tests._live_write_guard import (
    LiveEngineWriteBlocked,
    targets_live_engine,
)


# ── which hosts count as "the live engine" ────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://scaffold-orchestrator:8000/assist/abc/message",
    "http://scaffold-orchestrator:8000/work",
    "http://scaffold-orchestrator/anything",          # bare hostname
    "http://localhost:8000/health",
    "http://127.0.0.1:8000/v1/chat/completions",
    "http://0.0.0.0:8000/route",
])
def test_orchestrator_urls_are_blocked(url):
    assert targets_live_engine(url) is True


@pytest.mark.parametrize("url", [
    "http://172.18.0.1:11434/api/generate",   # Ollama on the bridge gateway
    "http://searxng:8888/search",
    "http://milvus-standalone:19530",
    "http://127.0.0.1:9099/pipelines",        # a local stub on another port
    "https://example.com/whatever",
    "",
    "not-a-url",
])
def test_other_hosts_are_untouched(url):
    """The guard must not become a blanket no-network rule — tests legitimately
    talk to stubs and non-orchestrator services on other ports."""
    assert targets_live_engine(url) is False


# ── the tripwire itself ───────────────────────────────────────────────────


def test_request_to_live_engine_raises(_ensure_guard):
    with pytest.raises(LiveEngineWriteBlocked) as exc:
        requests.post("http://scaffold-orchestrator:8000/assist/x/track", json={})
    msg = str(exc.value)
    # The message has to be actionable on its own — it is what a future
    # engineer sees at 2am when a test they did not write starts failing.
    assert "POST http://scaffold-orchestrator:8000/assist/x/track" in msg
    assert "Fix the TEST, not the guard" in msg
    assert "tests/integration/" in msg
    assert "SCAFFOLD_ALLOW_LIVE_TEST_WRITES" in msg


def test_reads_are_blocked_too(_ensure_guard):
    """A GET is not "safe": it proves the same non-hermeticity that lets a POST
    corrupt a session, and /work is precisely how the router lane found the
    operator's live session."""
    with pytest.raises(LiveEngineWriteBlocked):
        requests.get("http://scaffold-orchestrator:8000/work")


def test_non_orchestrator_calls_still_dispatch(_ensure_guard):
    """The guard must delegate, not swallow. Patch the underlying send so no
    real socket is opened, and assert the call went THROUGH the guard to it."""
    sentinel = MagicMock(name="response")
    with patch.object(_live_write_guard, "_original_request",
                      return_value=sentinel) as orig:
        out = requests.get("http://searxng:8888/search")
    assert out is sentinel
    assert orig.called


def test_install_is_idempotent(_ensure_guard):
    before = requests.Session.request
    _live_write_guard.install()
    _live_write_guard.install()
    assert requests.Session.request is before


def test_api_key_is_stripped_from_the_test_process(_ensure_guard):
    """Belt to the tripwire's braces: even a path that escapes the patch (a raw
    socket, a vendored client) must not be able to authenticate as the
    operator."""
    assert os.environ.get("SCAFFOLD_API_KEY", "") == ""


def test_escape_hatch_disables_install(monkeypatch):
    monkeypatch.setenv("SCAFFOLD_ALLOW_LIVE_TEST_WRITES", "1")
    _live_write_guard.uninstall()
    _live_write_guard.install()
    assert _live_write_guard._installed is False
    assert _live_write_guard.guard_disabled() is True


@pytest.fixture
def _ensure_guard():
    """The conftest autouse fixture already installs it for unguarded tests;
    this makes each case independent of ordering."""
    _live_write_guard.install()
    yield


def test_uninstall_restores_the_api_key():
    """`make test` runs unit and integration tests in ONE process. A unit test
    that stripped SCAFFOLD_API_KEY permanently would break every integration
    test scheduled after it, so the guard must be fully reversible."""
    real = _live_write_guard._saved_api_key
    if not real:
        pytest.skip("no SCAFFOLD_API_KEY in this environment to restore")
    _live_write_guard.install()
    assert os.environ["SCAFFOLD_API_KEY"] == ""
    _live_write_guard.uninstall()
    assert os.environ["SCAFFOLD_API_KEY"] == real
    _live_write_guard.install()  # leave the process guarded
