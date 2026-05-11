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

# Cloud-CI smoke runners can't route to 172.18.0.1 (the Docker bridge gateway
# that Pipeline.__init__ probes for embedder-dim verification). Local hosts
# happen to route to it. Without this stub, Pipeline() hangs the full
# request_timeout (300s) — capped at 30s per-test by pytest-timeout, but
# even that adds up to a workflow-budget kill across the 15+ tests that
# instantiate Pipeline. The stub is scoped to SCAFFOLD_CI_SMOKE_MODE so
# `make test` / `make ci` (dev image, has working Ollama) is unaffected.
if os.environ.get("SCAFFOLD_CI_SMOKE_MODE"):
    Pipeline._probe_embedder_dim = lambda self, model=None: (True, "ci-smoke stub")

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
