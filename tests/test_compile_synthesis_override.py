"""Sprint X.6 — per-job synthesis opt-in column.

Verifies:
  - `_resolve_synthesis_enabled` returns the per-job override when non-null,
    falling through to settings.compile_synthesis_enabled when NULL.
  - `_maybe_synthesize` honors the resolution: True forces on, False forces
    off, regardless of the global setting.
  - DB-error path fails open to the global setting (matches the synthesis
    fail-open contract).
  - `execution_status` (/exec/status) surfaces `synthesis_override` distinct
    from the existing `synthesized` (was-actually-synthesized) flag.

The migration + endpoint integration is covered live (migration 029 was
applied + the endpoint is exercised end-to-end via the SDK schemas).
This file isolates the resolution logic + status surface — the high-value
correctness checks.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import execution_compile


def _db_with_override(value):
    """Build an AsyncMock db whose first execute() returns a row with
    `compile_synthesis_override = value`.

    Subsequent execute() calls (e.g. by _synthesize_compiled_output) are
    not stubbed — tests that exercise those paths patch _synthesize_*
    directly so the call never reaches further DB use.
    """
    db = AsyncMock()
    row_result = MagicMock()
    row_result.scalar.return_value = value
    db.execute = AsyncMock(return_value=row_result)
    return db


@pytest.mark.smoke
class TestResolveSynthesisEnabled:
    """Per-job override resolution: True / False / NULL semantics."""

    async def test_override_true_forces_on_even_when_global_off(self):
        """jobs.compile_synthesis_override = TRUE → synthesis runs even
        when settings.compile_synthesis_enabled = False (global off)."""
        db = _db_with_override(True)
        with patch.object(settings, "compile_synthesis_enabled", False):
            result = await execution_compile._resolve_synthesis_enabled("jid", db)
        assert result is True

    async def test_override_false_forces_off_even_when_global_on(self):
        """jobs.compile_synthesis_override = FALSE → synthesis is skipped
        even when the global flag is True."""
        db = _db_with_override(False)
        with patch.object(settings, "compile_synthesis_enabled", True):
            result = await execution_compile._resolve_synthesis_enabled("jid", db)
        assert result is False

    async def test_null_inherits_global_on(self):
        """jobs.compile_synthesis_override IS NULL → fall through to global."""
        db = _db_with_override(None)
        with patch.object(settings, "compile_synthesis_enabled", True):
            result = await execution_compile._resolve_synthesis_enabled("jid", db)
        assert result is True

    async def test_null_inherits_global_off(self):
        db = _db_with_override(None)
        with patch.object(settings, "compile_synthesis_enabled", False):
            result = await execution_compile._resolve_synthesis_enabled("jid", db)
        assert result is False

    async def test_db_error_falls_open_to_global(self):
        """If the SELECT fails (transient connection drop, missing row),
        we fail open to the global setting — same fail-open pattern that
        already governs synthesis itself. Operators shouldn't lose
        synthesis output because of a flaky read on the override column."""
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch.object(settings, "compile_synthesis_enabled", True):
            result = await execution_compile._resolve_synthesis_enabled("jid", db)
        assert result is True

        db2 = AsyncMock()
        db2.execute = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch.object(settings, "compile_synthesis_enabled", False):
            result2 = await execution_compile._resolve_synthesis_enabled("jid", db2)
        assert result2 is False


@pytest.mark.smoke
class TestMaybeSynthesizeHonorsOverride:
    """End-to-end: _maybe_synthesize must consult the override before
    running the LLM pass. The override bypasses settings entirely."""

    async def test_override_true_runs_synthesis_when_global_off(self):
        """Global off + override True → synthesis runs, returns synthesized
        text and was_synthesized=True."""
        db = _db_with_override(True)
        with patch.object(settings, "compile_synthesis_enabled", False), \
             patch.object(
                 execution_compile, "_synthesize_compiled_output",
                 new=AsyncMock(return_value="LLM-rewritten narrative"),
             ):
            text_value, was_syn = await execution_compile._maybe_synthesize(
                job_id="jid", heuristic="raw heuristic body",
                strategy="0_single_leaf", source_tool="LLM", db=db,
            )
        assert text_value == "LLM-rewritten narrative"
        assert was_syn is True

    async def test_override_false_skips_synthesis_when_global_on(self):
        """Global on + override False → heuristic returned unchanged,
        was_synthesized=False. The LLM pass is never called."""
        db = _db_with_override(False)
        synth_mock = AsyncMock(return_value="this should never be returned")
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch.object(execution_compile, "_synthesize_compiled_output", new=synth_mock):
            text_value, was_syn = await execution_compile._maybe_synthesize(
                job_id="jid", heuristic="raw heuristic body",
                strategy="0_single_leaf", source_tool="LLM", db=db,
            )
        assert text_value == "raw heuristic body"
        assert was_syn is False
        synth_mock.assert_not_called()


@pytest.mark.smoke
class TestExecutionStatusSurface:
    """`execution_status` must include `synthesis_override` alongside the
    existing `synthesized` flag — distinct semantics (knob-state vs.
    last-compile-result)."""

    async def test_status_includes_synthesis_override_field(self):
        from app.modules.execution_handler import execution_status

        # Two-result mock: first SELECT (job row), second SELECT (nodes).
        job_row = SimpleNamespace(
            id="job-1", title="x", status="completed",
            compiled_output="...",
            compiled_output_synthesized=True,
            compile_synthesis_override=True,
        )
        job_result = MagicMock()
        job_result.fetchone.return_value = job_row
        nodes_result = MagicMock()
        nodes_result.fetchall.return_value = []  # no nodes; counts/total = 0

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[job_result, nodes_result])

        result = await execution_status("00000000-0000-0000-0000-000000000001", db)

        assert "synthesis_override" in result
        assert result["synthesis_override"] is True
        # Distinct from the existing "did the last compile run synthesis" flag.
        assert "synthesized" in result
        assert result["synthesized"] is True

    async def test_status_synthesis_override_null_renders_null(self):
        """The default state (override never set) surfaces as None — the
        clients render this as "inherits global" / "auto"."""
        from app.modules.execution_handler import execution_status

        job_row = SimpleNamespace(
            id="job-1", title="x", status="planning",
            compiled_output=None,
            compiled_output_synthesized=False,
            compile_synthesis_override=None,
        )
        job_result = MagicMock()
        job_result.fetchone.return_value = job_row
        nodes_result = MagicMock()
        nodes_result.fetchall.return_value = []

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[job_result, nodes_result])

        result = await execution_status("00000000-0000-0000-0000-000000000002", db)

        assert result["synthesis_override"] is None
        assert result["synthesized"] is False
