"""Shared module-loading helper for test_scaffold_router_*.py files (#9.6).

The scaffold_router lives in pipelines/ which isn't a Python package, so
tests must load it via importlib. This helper does the load once and
exposes `_mod`, `Pipeline`, `_router_path`, and `_make_response` for any
split test file to import.

If the scaffold_router source isn't present (e.g. inside the orchestrator
container where only app/ is mounted), this module calls pytest.skip with
allow_module_level=True — any test file that imports from here will be
cleanly skipped by pytest rather than failing with NoneType errors.

Not a conftest — pytest shouldn't collect it (leading underscore).
"""
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests import _live_write_guard

# §17.934 — install BEFORE the router module is exec'd or any Pipeline is
# built. This lane runs with --noconftest (tests/conftest.py eager-loads app
# and shadows the pipeline mocks), so the conftest-level guard never loads
# here — but every test_scaffold_router_*.py file imports THIS helper, which
# makes it the lane's one reliable chokepoint.
#
# This is the lane that actually did the damage: it drove real turns through
# the live pipeline and §17.770 bound them to the operator's active assist
# session. It is a UNIT lane; it has no business reaching the engine at all.
_live_write_guard.install()

_router_candidates = [
    Path(__file__).resolve().parent.parent / "pipelines" / "scaffold_router.py",
    Path("/app/pipelines/scaffold_router.py"),
]
_router_path = None
for _p in _router_candidates:
    if _p.exists():
        _router_path = _p
        break

if _router_path is None:
    pytest.skip(
        "scaffold_router.py not found — skipping (expected in pipelines/ directory)",
        allow_module_level=True,
    )

spec = importlib.util.spec_from_file_location("scaffold_router", _router_path)
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
Pipeline = _mod.Pipeline

# §17.333 — Unconditional stub. `Pipeline.__init__` calls
# `_probe_embedder_dim` which does a real HTTP POST to Ollama with a
# 300s timeout. Three failure modes the stub eliminates:
#
#   (1) Cloud-CI smoke runners can't route to 172.18.0.1 — the original
#       reason this stub existed, gated on SCAFFOLD_CI_SMOKE_MODE.
#   (2) Local-host suite-wide Ollama queue contention. The probe is fast
#       in isolation (~0.2 s) but stalls past pytest-timeout's 30 s when
#       15+ tests instantiate Pipeline back-to-back AND other tests hold
#       Ollama. Observed once as a flake at
#       `TestRememberRecallHelpers::test_remember_then_recall_returns_job_id`
#       (the first test in a class that builds a fresh Pipeline; the
#       subsequent 12 tests cached the instance and all passed).
#   (3) Operators running `make test` on a host where Ollama is
#       temporarily down — every Pipeline-instantiating test fails on
#       __init__ even if the assertion would pass under nominal
#       conditions.
#
# All three are integration concerns, not unit-test concerns. The live
# embedder-dim invariant is verified by `/health`'s probe + the
# `tests/integration/` suite. The stub returns ok=True with a marker
# string so any `Embedder probe OK: <msg>` log line continues to render.
Pipeline._probe_embedder_dim = lambda self, model=None: (
    True, "test stub (§17.333)"
)

# §17.934 — same treatment, same reason, for the OTHER unconditional live call
# on the pipe() path. `_fetch_work` GETs /work on essentially every turn to
# back the §17.770 sole-active-session binding, and it is what dragged this
# UNIT lane onto the operator's real engine: the probe found their live assist
# session and the lane's fixtures were bound to it as durable turns.
#
# None is the method's OWN documented degrade value ("returns None on any
# error so callers degrade gracefully"), so the default is the no-work path
# rather than an invented shape. Any test that cares about work state patches
# the instance, which takes precedence over this class attribute.
Pipeline._fetch_work = lambda self: None

# §17.934 — and the third unconditional live call on the pipe() path.
# `fetch_assist_candidates` GETs /assist/candidates; it is reached from
# `_reconnect_in_progress` and `_in_progress_banner`, i.e. on nearly every
# turn. Stubbing it at the NETWORK seam rather than at its two callers is
# deliberate: the pipe-level continuity logic still executes and stays under
# test, it just does so against an empty candidate list instead of the
# operator's real in-flight jobs. `[]` is the function's own fail-soft return.
_mod._assist.fetch_assist_candidates = lambda pipe: []

# §17.934 — and the fourth: `_classify_command` POSTs /route on every
# natural-language turn. Its OWN fail-soft return is intent='none' ("a
# classifier or endpoint hiccup degrades to triage rather than misfiring"),
# which is exactly the neutral default a unit wants — the tests this affects
# are the `falls_to_triage` / `does_not_hijack` cases, i.e. they assert the
# fall-through this produces. `_nl_command_route`'s own gating logic stays
# under test; only the network hop is replaced. Tests that need a specific
# intent patch the instance, which wins over this class attribute.
Pipeline._classify_command = lambda self, msg: {"intent": "none", "confidence": "low"}

sys.modules["scaffold_router"] = _mod
_pkg = types.ModuleType("pipelines")
_pkg.scaffold_router = _mod
sys.modules["pipelines"] = _pkg
sys.modules["pipelines.scaffold_router"] = _mod


def _make_response(status_code: int, body: dict | str = "") -> MagicMock:
    """Create a fake requests.Response object for testing.

    Used by TestFmt (helpers file) and TestHandleCommand (commands file).
    """
    r = MagicMock()
    r.status_code = status_code
    if isinstance(body, dict):
        r.json.return_value = body
        r.text = json.dumps(body)
    else:
        r.json.side_effect = ValueError("No JSON")
        r.text = body
    return r
