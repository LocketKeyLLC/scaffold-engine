"""§17.934 — stop the test suite writing into the operator's LIVE engine.

`make test` runs INSIDE the orchestrator container. That container exports
`SCAFFOLD_API_KEY`, and `scaffold-orchestrator:8000` resolves to itself — so a
test that reaches the network authenticates as the MASTER key against the real
database. `pipelines/scaffold_router.py` needs no valve to get there:
`_auth_headers()` falls back to `os.getenv("SCAFFOLD_API_KEY", "")` and
`orchestrator_url` already defaults to `http://scaffold-orchestrator:8000`.

What that cost, twice, on the operator's box: the `test_scaffold_router_*` lane
drove real turns through the live pipeline, and §17.770 sticky-continuity bound
them to the sole active assist session. Its fixtures — *"I want to build a
markdown linter"*, *"anything"*, *"assist with the completion and
implementation of the homelab"* — landed as DURABLE `assist_turns` on the
operator's in-flight steps (61 rows across 2026-08-31 → 09-05, removed by
hand). It was already known that this lane FAILS when a live session exists;
that it also WRITES was not.

That matters more after §17.928: the transcript window is now the NEWEST turns,
so injected fixtures are precisely what the model reads as current context.
This is `feedback_never_verify_with_synthetic_input` arriving by a back door —
nobody typed the synthetic input, the test lane typed it.

The guard is deliberately NOISY rather than silent. Returning a canned 401
would leave a non-hermetic test half-working and hide the dependency; raising
names the offending URL and the test that made the call, so the fix is obvious.

Exemptions:
  * `tests/integration/` legitimately drives live services — the conftest hook
    skips anything marked `integration`.
  * `SCAFFOLD_ALLOW_LIVE_TEST_WRITES=1` disables the guard wholesale, for
    deliberately driving the live engine from a throwaway box.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

#: Hosts that ARE the operator's live engine. A request to any of these from a
#: unit test is a bug regardless of method — a GET reveals the same
#: non-hermeticity that lets a POST corrupt a session.
_LIVE_HOSTS = {
    "scaffold-orchestrator",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
}
_LIVE_PORTS = {8000, None}

_ENV_ESCAPE = "SCAFFOLD_ALLOW_LIVE_TEST_WRITES"


class LiveEngineWriteBlocked(RuntimeError):
    """A unit test tried to reach the operator's live orchestrator."""


def guard_disabled() -> bool:
    return os.getenv(_ENV_ESCAPE, "").strip().lower() in ("1", "true", "yes", "on")


def targets_live_engine(url: str) -> bool:
    """True when `url` points at the live orchestrator.

    Port-aware so a test hitting, say, Ollama on 172.18.0.1:11434 or a local
    stub on 127.0.0.1:9099 is not swept up — only the orchestrator's own
    surface is off-limits.
    """
    try:
        parsed = urlparse(url if "//" in str(url) else f"//{url}")
    except Exception:  # noqa: BLE001 — an unparseable URL is not our business
        return False
    host = (parsed.hostname or "").lower()
    if host not in _LIVE_HOSTS:
        return False
    port = parsed.port
    # A bare orchestrator hostname with no port still means the engine.
    if host == "scaffold-orchestrator":
        return True
    return port in _LIVE_PORTS


def _explain(method: str, url: str) -> str:
    return (
        f"\n\n§17.934 BLOCKED: a test tried to call the LIVE orchestrator.\n"
        f"    {method} {url}\n\n"
        "This process runs inside the orchestrator container, so that request "
        "would authenticate with the operator's master SCAFFOLD_API_KEY and "
        "write to the real database — this is how the scaffold_router lane "
        "injected 61 fixture turns into a live assist session.\n\n"
        "Fix the TEST, not the guard: mock the HTTP call (`patch.object(pipe, "
        "'_call_...')` or patch `requests.post`) so the unit stays hermetic.\n"
        "If this test genuinely needs live services it belongs in "
        "tests/integration/ (marked `integration`, which is exempt).\n"
        f"To bypass deliberately on a throwaway box: {_ENV_ESCAPE}=1\n"
    )


_installed = False
_original_request = None
#: The real key, stashed on first install so `uninstall()` can hand it back.
#: `make test` runs unit and integration tests in ONE process, so a unit test
#: that strips the key permanently would break every integration test that
#: happens to run after it — the guard must be fully reversible, not just
#: patch-reversible.
_saved_api_key: str | None = None


def install() -> None:
    """Patch `requests.Session.request` — the chokepoint every `requests` entry
    point funnels through, module-level `requests.post` included (it builds a
    Session internally). Idempotent.

    Also clears `SCAFFOLD_API_KEY` from the test process so that even a code
    path that escapes the patch (a socket call, a vendored client) cannot
    authenticate as the operator.
    """
    global _installed, _original_request, _saved_api_key
    if _installed or guard_disabled():
        return

    # Belt: without the master key an escaped request can only 401, never write.
    if _saved_api_key is None:
        _saved_api_key = os.environ.get("SCAFFOLD_API_KEY", "")
    os.environ["SCAFFOLD_API_KEY"] = ""

    try:
        import requests
    except ImportError:  # pragma: no cover — requests is a hard dep here
        return

    _original_request = requests.Session.request

    def _guarded(self, method, url, *args, **kwargs):
        if targets_live_engine(str(url)):
            raise LiveEngineWriteBlocked(_explain(str(method).upper(), str(url)))
        return _original_request(self, method, url, *args, **kwargs)

    requests.Session.request = _guarded
    _installed = True


def uninstall() -> None:
    """Restore the real `requests.Session.request` (used by the conftest hook to
    exempt `integration`-marked tests)."""
    global _installed
    if _saved_api_key is not None:
        os.environ["SCAFFOLD_API_KEY"] = _saved_api_key
    if not _installed or _original_request is None:
        return
    import requests

    requests.Session.request = _original_request
    _installed = False
