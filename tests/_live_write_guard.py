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

§17.944 — the guard also blocks LIVE INFERENCE.

§17.943 was a unit test calling Ollama: `grounding_gate_enabled` defaults True,
so a synthesis-override test ran `score_faithfulness` for real, and when the
model happened to score low the gate prepended a banner to the value the test
asserted on. It failed one run in five for weeks.

A sweep instrumented every outbound httpx call across the lane. Fifteen tests
in six files were reaching `POST /api/chat` or `/api/generate`; twenty-one more
made Ollama GET probes. But blocking Ollama and re-running the FULL lane gave
**5,241 passed / 1 skipped / 0 failed** — byte-identical to the unblocked
baseline. Not one unit test's assertions actually depend on live model output;
they mock the call and assert the mocked return, while an unmocked side-path
quietly talks to the model anyway.

So inference is dead weight in this lane: it costs seconds per test, makes the
suite fail when Ollama is down, and — the §17.943 case — occasionally lets a
real model response reach an assertion. Blocking it is free, and it converts
that whole class from "flakes five runs later" into "fails here, by name".

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


def _inference_netloc() -> tuple[str, int | None]:
    """§17.944 — (host, port) of the configured model endpoint.

    Read from settings rather than hardcoded: `ollama_base_url` is
    deployment-specific (172.18.0.1:11434 on this box, because containers reach
    the host Ollama through the bridge gateway) and a guard pinned to one
    address would silently stop guarding anywhere else.
    """
    try:
        from app.config import settings

        parsed = urlparse(str(settings.ollama_base_url))
        return (parsed.hostname or "").lower(), parsed.port
    except Exception:  # noqa: BLE001 — never break collection over a guard
        return "", None


def targets_inference(url: str) -> bool:
    """True when `url` points at the configured model endpoint."""
    host, port = _inference_netloc()
    if not host:
        return False
    try:
        parsed = urlparse(url if "//" in str(url) else f"//{url}")
    except Exception:  # noqa: BLE001
        return False
    return (parsed.hostname or "").lower() == host and parsed.port == port


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


#: §17.945 — the simulation sidecars. Nothing in the unit lane legitimately
#: reaches them: the adapter tests stub `httpx.MockTransport` and the health
#: tests hand in MagicMock clients. Listed so that if a unit test ever DOES
#: open a real connection to one, it says so by name.
_SIDECAR_HOSTS = {
    "scaffold-ngspice", "ngspice",
    "scaffold-verilator", "verilator",
    "scaffold-symbiyosys", "symbiyosys",
    "scaffold-coderunner", "coderunner",
}


def targets_sidecar(url: str) -> bool:
    try:
        parsed = urlparse(url if "//" in str(url) else f"//{url}")
    except Exception:  # noqa: BLE001
        return False
    return (parsed.hostname or "").lower() in _SIDECAR_HOSTS


def _explain_sidecar(method: str, url: str) -> str:
    return (
        f"\n\n§17.945 BLOCKED: a unit test opened a real connection to a "
        f"simulation sidecar.\n    {method} {url}\n\n"
        "No unit test needs one. The adapter tests stub `httpx.MockTransport` "
        "(a real AsyncClient with a fake transport, which this guard "
        "deliberately does NOT intercept) and the health tests hand in "
        "MagicMock clients — both stay hermetic without a live sidecar.\n\n"
        "Fix the TEST: stub the transport or mock the client getter, the way "
        "tests/test_sim_ngspice_adapter.py and tests/test_health_cleanup.py "
        "already do. If it genuinely needs a running sidecar it belongs in "
        "tests/integration/ (marked `integration`, which is exempt).\n"
        f"To bypass deliberately on a throwaway box: {_ENV_ESCAPE}=1\n"
    )


def _explain_inference(method: str, url: str) -> str:
    return (
        f"\n\n§17.944 BLOCKED: a unit test tried to call LIVE INFERENCE.\n"
        f"    {method} {url}\n\n"
        "No unit test's assertions depend on real model output — the whole lane "
        "passes with this blocked (5,241 passed, measured). A live call here is "
        "dead weight at best, and at worst it is §17.943: a real response "
        "reaching an assertion and failing one run in five.\n\n"
        "Fix the TEST: mock the model call. Patch the specific function the code "
        "under test calls (`model_router.generate` / `.chat` / `.tool_call`, or "
        "the helper that wraps it), or pin the valve that gates it — "
        "`grounding_gate_enabled` is the one that caused §17.943.\n"
        "If it genuinely needs a model it belongs in tests/integration/ "
        "(marked `integration`, which is exempt).\n"
        f"To bypass deliberately on a throwaway box: {_ENV_ESCAPE}=1\n"
    )


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
#: §17.944/§17.945 — the app talks to the orchestrator with `requests` and to
#: the model with `httpx`, so the guard needs both.
#:
#: §17.945 — the httpx hook is on the TRANSPORT, not on `AsyncClient.send`.
#: `send` sits ABOVE the transport, so a test using `httpx.MockTransport` — a
#: real AsyncClient with a stubbed transport, which is exactly how the sim
#: adapter tests stay hermetic — was intercepted even though its request was
#: never going to leave the process. Blocking at `send` failed 11 already-
#: correct tests. `AsyncHTTPTransport.handle_async_request` is real network
#: egress; MockTransport is a different class and passes through untouched.
_original_httpx_transport = None
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
    global _installed, _original_request, _original_httpx_transport, _saved_api_key
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

    # §17.944 — inference goes out over httpx, not requests.
    try:
        import httpx
    except ImportError:  # pragma: no cover — httpx is a hard dep here
        _installed = True
        return

    _original_httpx_transport = httpx.AsyncHTTPTransport.handle_async_request

    async def _guarded_transport(self, request, *a, **kw):
        url = str(request.url)
        if targets_inference(url):
            raise LiveEngineWriteBlocked(
                _explain_inference(str(request.method).upper(), url))
        if targets_live_engine(url):
            raise LiveEngineWriteBlocked(
                _explain(str(request.method).upper(), url))
        if targets_sidecar(url):
            raise LiveEngineWriteBlocked(
                _explain_sidecar(str(request.method).upper(), url))
        return await _original_httpx_transport(self, request, *a, **kw)

    httpx.AsyncHTTPTransport.handle_async_request = _guarded_transport
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
    if _original_httpx_transport is not None:
        import httpx

        httpx.AsyncHTTPTransport.handle_async_request = _original_httpx_transport
    _installed = False
