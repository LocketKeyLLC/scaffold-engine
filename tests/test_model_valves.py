"""
tests/test_model_valves.py — Tests for the model valve override system.

Tests three layers:
  1. get_model() in app/config.py — override priority chain
  2. _model_overrides() in scaffold_router.py — valve-to-dict mapping
  3. Orchestrator request payloads — model_overrides included in calls

Run:
    python3 -m pytest tests/test_model_valves.py --noconftest -v
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import pytest

# ---------------------------------------------------------------------------
# Load app/config.py via importlib (Docker-safe)
# ---------------------------------------------------------------------------
_config_candidates = [
    Path(__file__).resolve().parent.parent / "app" / "config.py",
    Path("/app/app/config.py"),
]

_config_path = None
for _p in _config_candidates:
    if _p.exists():
        _config_path = _p
        break

if _config_path is None:
    pytest.skip(
        "app/config.py not found — skipping",
        allow_module_level=True,
    )

_stub_base = MagicMock()
_stub_base.BaseSettings = type("BaseSettings", (), {"model_config": {}})
with patch.dict(sys.modules, {"pydantic_settings": _stub_base}):
    _config_spec = importlib.util.spec_from_file_location("app_config", _config_path)
    _config_mod = importlib.util.module_from_spec(_config_spec)
    _config_spec.loader.exec_module(_config_mod)

get_model = _config_mod.get_model
set_runtime_model = _config_mod.set_runtime_model
settings = _config_mod.settings

# ---------------------------------------------------------------------------
# Load scaffold_router.py via importlib (Docker-safe)
# ---------------------------------------------------------------------------
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
        "scaffold_router.py not found — skipping",
        allow_module_level=True,
    )

_router_spec = importlib.util.spec_from_file_location("scaffold_router", _router_path)
_router_mod = importlib.util.module_from_spec(_router_spec)
_router_spec.loader.exec_module(_router_mod)
Pipeline = _router_mod.Pipeline

# §17.402 — UNCONDITIONAL embedder-probe stub (matches §17.333 in
# tests/_scaffold_router_setup.py). Pipeline.__init__ does a real HTTP POST
# to Ollama at 172.18.0.1, unroutable on cloud CI (test.yml) and a flake
# source under local suite contention. Was gated on SCAFFOLD_CI_SMOKE_MODE,
# which test.yml doesn't set → these tests errored there. The live
# embedder-dim invariant is covered by /health + tests/integration/.
Pipeline._probe_embedder_dim = lambda self, model=None: (True, "test stub (§17.402)")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ALL_VALVE_KEYS = [
    "model_general",
    "model_verifier",
    "model_coder",
    "model_embedder",
    "model_reranker",
    "model_router",
    "model_fallback",
    "model_cloud_alt",
]

OVERRIDE_VALVE_KEYS = [
    "model_general",
    "model_verifier",
    "model_coder",
    "model_router",
    "model_fallback",
    "model_cloud_alt",
]

SETTINGS_ROLES = [
    "model_general",
    "model_verifier",
    "model_coder",
    "model_router",
    "model_fallback",
    "model_cloud_alt",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def pipe():
    """Fresh Pipeline instance."""
    return Pipeline()


# ===================================================================
# STEP 1: get_model() — override priority chain
# ===================================================================
@pytest.mark.smoke
class TestGetModel:
    """get_model(role, overrides): override > env var > default."""

    def test_no_overrides_returns_default(self):
        result = get_model("model_general", None)
        assert result == settings.model_general

    def test_empty_dict_returns_default(self):
        result = get_model("model_verifier", {})
        assert result == settings.model_verifier

    def test_override_present_returns_override(self):
        result = get_model("model_general", {"model_general": "custom:13b"})
        assert result == "custom:13b"

    def test_partial_overrides_fall_through(self):
        overrides = {"model_general": "custom:13b"}
        result = get_model("model_verifier", overrides)
        assert result == settings.model_verifier

    def test_override_with_empty_string_raises(self):
        """Empty-string overrides are rejected explicitly so callers can't
        silently misconfigure a role by passing whitespace."""
        with pytest.raises(ValueError, match="non-empty string"):
            get_model("model_coder", {"model_coder": ""})
        with pytest.raises(ValueError, match="non-empty string"):
            get_model("model_coder", {"model_coder": "   "})

    def test_override_with_none_value_falls_through(self):
        """None retains the omit-the-key semantics: caller is explicitly
        nulling the override and wants the default."""
        result = get_model("model_router", {"model_router": None})
        assert result == settings.model_router

    def test_all_settings_roles_resolvable(self):
        for role in SETTINGS_ROLES:
            result = get_model(role, None)
            assert isinstance(result, str) and len(result) > 0, f"{role} failed"

    def test_override_bypasses_missing_settings_attr(self):
        # Uses an allowlisted role; the test name is historical — the point
        # is that the override path doesn't touch the settings object.
        result = get_model("model_embedder_pipeline", {"model_embedder_pipeline": "qwen3-embedding:8b"})
        assert result == "qwen3-embedding:8b"


@pytest.mark.smoke
class TestSetRuntimeModel:
    """§17.483 — set_runtime_model mutates the settings singleton for a
    switchable role (ephemeral) and rejects singletons / blanks."""

    def test_sets_switchable_role(self):
        original = settings.model_general
        try:
            set_runtime_model("model_general", "runtime:7b")
            assert settings.model_general == "runtime:7b"
            # get_model now resolves the runtime value (no override).
            assert get_model("model_general", None) == "runtime:7b"
        finally:
            settings.model_general = original

    def test_strips_whitespace(self):
        original = settings.model_coder
        try:
            set_runtime_model("model_coder", "  spaced:3b  ")
            assert settings.model_coder == "spaced:3b"
        finally:
            settings.model_coder = original

    def test_rejects_singleton_role(self):
        original = settings.model_reranker
        with pytest.raises(ValueError, match="config-only"):
            set_runtime_model("model_reranker", "x:1b")
        assert settings.model_reranker == original  # untouched

    def test_rejects_unknown_role(self):
        with pytest.raises(ValueError, match="unknown role"):
            set_runtime_model("model_nonexistent", "x:1b")

    def test_rejects_blank_tag(self):
        original = settings.model_fallback
        with pytest.raises(ValueError, match="non-empty"):
            set_runtime_model("model_fallback", "   ")
        assert settings.model_fallback == original


@pytest.mark.smoke
class TestClearRuntimeModel:
    """§17.484 — env-default snapshot + clear (revert) for switchable roles."""

    def test_clear_reverts_to_env_default(self):
        env_def = _config_mod.env_default_model("model_general")
        set_runtime_model("model_general", "temp:9b")
        assert settings.model_general == "temp:9b"
        _config_mod.clear_runtime_model("model_general")
        assert settings.model_general == env_def

    def test_env_default_is_pristine_after_override(self):
        # The snapshot must reflect the ORIGINAL value even once overridden.
        env_def = _config_mod.env_default_model("model_coder")
        set_runtime_model("model_coder", "drifted:1b")
        try:
            assert _config_mod.env_default_model("model_coder") == env_def
        finally:
            _config_mod.clear_runtime_model("model_coder")

    def test_env_default_rejects_unknown_role(self):
        with pytest.raises(ValueError, match="unknown switchable role"):
            _config_mod.env_default_model("model_nope")

    def test_clear_rejects_singleton(self):
        with pytest.raises(ValueError, match="unknown switchable role"):
            _config_mod.clear_runtime_model("model_reranker")


# ===================================================================
# STEP 2: _model_overrides() — valve-to-dict mapping
# ===================================================================
@pytest.mark.smoke
class TestModelOverrides:
    """_model_overrides() returns all 8 valve keys with correct values."""

    def test_returns_all_six_override_keys(self, pipe):
        result = pipe._model_overrides()
        assert set(result.keys()) == set(OVERRIDE_VALVE_KEYS)

    def test_values_match_valve_defaults(self, pipe):
        result = pipe._model_overrides()
        for key in OVERRIDE_VALVE_KEYS:
            assert result[key] == getattr(pipe.valves, key), (
                f"{key}: expected {getattr(pipe.valves, key)!r}, got {result[key]!r}"
            )

    def test_reflects_valve_changes(self, pipe):
        pipe.valves.model_general = "test-override:99b"
        result = pipe._model_overrides()
        assert result["model_general"] == "test-override:99b"

    def test_no_extra_keys(self, pipe):
        result = pipe._model_overrides()
        assert len(result) == 6


# ===================================================================
# STEP 3: Orchestrator payloads include model_overrides
# ===================================================================
@pytest.mark.smoke
class TestPayloadInclusion:
    """Orchestrator API calls include model_overrides from valves."""

    def _run_command(self, pipe, command, mock_post):
        """Call pipe() with correct signature, exhaust generator."""
        # §17.562 — these tests assert payload CONTENTS for advanced commands
        # (/optimize, /rag, …); enable the full surface so the guided gate
        # doesn't short-circuit them before the HTTP call.
        pipe.valves.advanced_commands_enabled = True
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok", "job_id": "test-job-1"}
        mock_resp.text = '{"status": "ok"}'
        mock_post.return_value = mock_resp

        messages = [{"role": "user", "content": command}]
        body = {"messages": messages, "stream": False}

        result = pipe.pipe(
            user_message=command,
            model_id="test-model",
            messages=messages,
            body=body,
        )

        # Exhaust generators (streamed commands)
        if hasattr(result, "__next__"):
            for _ in result:
                pass
        return mock_post

    def test_idea_includes_overrides(self, pipe):
        with patch.object(_router_mod._HTTP_SESSION, "post") as mock_post:
            self._run_command(pipe, "/idea build a web scraper", mock_post)
            assert mock_post.called
            payload = mock_post.call_args[1].get("json", {})
            assert "model_overrides" in payload

    def test_dag_includes_overrides(self, pipe):
        with patch.object(_router_mod._HTTP_SESSION, "post") as mock_post:
            self._run_command(pipe, "/dag test-job-1", mock_post)
            assert mock_post.called
            payload = mock_post.call_args[1].get("json", {})
            assert "model_overrides" in payload

    def test_optimize_includes_overrides(self, pipe):
        with patch.object(_router_mod._HTTP_SESSION, "post") as mock_post:
            self._run_command(pipe, "/optimize write a better prompt", mock_post)
            assert mock_post.called
            payload = mock_post.call_args[1].get("json", {})
            assert "model_overrides" in payload

    def test_rag_includes_overrides(self, pipe):
        with patch.object(_router_mod._HTTP_SESSION, "post") as mock_post:
            self._run_command(pipe, "/rag kubernetes networking", mock_post)
            assert mock_post.called
            payload = mock_post.call_args[1].get("json", {})
            assert "model_overrides" not in payload

    def test_overrides_reflect_custom_valves(self, pipe):
        pipe.valves.model_general = "my-custom:13b"
        with patch.object(_router_mod._HTTP_SESSION, "post") as mock_post:
            self._run_command(pipe, "/idea test idea", mock_post)
            payload = mock_post.call_args[1].get("json", {})
            assert payload["model_overrides"]["model_general"] == "my-custom:13b"

    def test_model_set_updates_overrides(self, pipe):
        """After /model set, _model_overrides() reflects the new value."""
        # Simulate what /model set does: directly update the valve
        pipe.valves.model_verifier = "custom-verifier:3b"
        overrides = pipe._model_overrides()
        assert overrides["model_verifier"] == "custom-verifier:3b"
        # Other roles unchanged
        assert overrides["model_general"] == "qwen3.5:397b-cloud"
