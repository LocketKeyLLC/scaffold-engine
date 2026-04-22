"""Tests for scaffold_router.py — code-structure/static-analysis regression guards.

Split from the original test_scaffold_router.py (#9.6).
Shared module-loading logic lives in _scaffold_router_setup.py.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import _mod, Pipeline, _router_path


@pytest.fixture
def pipe():
    """Fresh Pipeline instance per test."""
    return Pipeline()


@pytest.mark.smoke
class TestModelOverridesSingleSource:
    """#8.10: _MODEL_DEFAULTS removed; defaults read from self.Valves()."""

    def test_no_model_defaults_attribute(self, pipe):
        assert not hasattr(pipe, "_MODEL_DEFAULTS")

    def test_overrides_filter_empty_strings(self, pipe):
        pipe.valves.model_general = ""
        pipe.valves.model_coder = "qwen2.5-coder:7b"
        ov = pipe._model_overrides()
        assert "model_general" not in ov
        assert ov.get("model_coder") == "qwen2.5-coder:7b"


class TestNoPrintStatements:
    """#8.11: scaffold_router.py uses logger, not print()."""

    def test_source_has_no_print(self):
        src = _router_path.read_text()
        offenders = [
            ln for ln in src.splitlines()
            if "print(" in ln and not ln.lstrip().startswith("#")
        ]
        assert offenders == [], "Unexpected print() calls:\n" + "\n".join(offenders)


@pytest.mark.smoke
class TestTimeoutValveConsolidation:
    """#8.8: three timeout valves; legacy dag_timeout alias preserved."""

    def test_three_timeout_valves_exist(self, pipe):
        assert hasattr(pipe.valves, "request_timeout")
        assert hasattr(pipe.valves, "stream_timeout")
        assert hasattr(pipe.valves, "triage_timeout")

    def test_dag_timeout_alias_still_present(self, pipe):
        assert hasattr(pipe.valves, "dag_timeout")

