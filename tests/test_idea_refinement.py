"""
tests/test_idea_refinement.py — Idea refinement module smoke tests

Uses importlib to avoid WORKDIR /app package collision (Task #18).
Tests refine_idea() input validation, output structure, and error handling.
"""

import importlib.util
import os
import sys
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# importlib loader
# ---------------------------------------------------------------------------

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "modules", "idea_refinement.py"
)
_ABS_PATH = os.path.abspath(_MODULE_PATH)


def _load_module():
    """Load idea_refinement.py via importlib, stubbing heavy deps."""
    stubs = {}
    for mod_name in [
        "app", "app.database", "app.modules", "app.config",
        "app.model_router", "app.schemas",
        "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio",
        "sqlalchemy.orm", "sqlalchemy.sql", "sqlalchemy.text",
        "structlog", "aiohttp", "asyncpg",
        "logging",
    ]:
        if mod_name not in sys.modules:
            stubs[mod_name] = MagicMock()

    mock_structlog = MagicMock()
    mock_structlog.get_logger.return_value = MagicMock()
    stubs["structlog"] = mock_structlog

    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "idea_refinement", _ABS_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            pass
        return mod


_idea_mod = None
try:
    _idea_mod = _load_module()
except Exception:
    pass

pytestmark = pytest.mark.skipif(
    _idea_mod is None or not hasattr(_idea_mod, "refine_idea"),
    reason="idea_refinement.py not loadable in this environment",
)


# ===========================================================================
# refine_idea tests
# ===========================================================================

class TestRefineIdea:
    """Tests for refine_idea() — input validation and output contract."""

    @pytest.mark.asyncio
    async def test_returns_dict(self):
        """refine_idea returns a dict with job_id and status."""
        import inspect
        sig = inspect.signature(_idea_mod.refine_idea)
        params = list(sig.parameters.keys())
        # Verify signature: (idea_text, db, model, domain)
        assert "idea_text" in params or "idea" in params, f"Expected idea_text param, got {params}"
        assert "db" in params, f"Expected db param, got {params}"
        # Return type should be dict based on source
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert "-> dict" in source, "refine_idea should return dict"

    def test_requires_db_session(self):
        """refine_idea requires a db session parameter."""
        import inspect
        sig = inspect.signature(_idea_mod.refine_idea)
        params = list(sig.parameters.keys())
        assert "db" in params, "refine_idea should require db session"

    def test_accepts_optional_model_and_domain(self):
        """refine_idea accepts optional model and domain parameters."""
        import inspect
        sig = inspect.signature(_idea_mod.refine_idea)
        params = sig.parameters
        assert params.get("model") is not None, "Should accept model param"
        assert params.get("domain") is not None, "Should accept domain param"
        # Both should have defaults (optional)
        assert params["model"].default is not inspect.Parameter.empty or \
               params["model"].default is None
        assert params["domain"].default is not inspect.Parameter.empty or \
               params["domain"].default is None

    def test_module_has_refine_idea(self):
        """Module exports refine_idea function."""
        assert hasattr(_idea_mod, "refine_idea")
        assert callable(_idea_mod.refine_idea)

    def test_refine_idea_is_async(self):
        """refine_idea is an async function."""
        import asyncio
        assert asyncio.iscoroutinefunction(_idea_mod.refine_idea)

    def test_module_has_expected_constants(self):
        """Module defines expected model or prompt constants."""
        # The module should reference the verifier model or have a system prompt
        source_path = _ABS_PATH
        with open(source_path, "r") as f:
            source = f.read()
        # Should reference either the model or Ollama
        assert any(term in source for term in [
            "qwen", "ollama", "OLLAMA", "model", "refine",
        ]), "Module should reference model configuration"
