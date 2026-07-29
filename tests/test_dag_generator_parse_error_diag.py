"""§17.293 — DAG generator surfaces JSONDecodeError diagnostics on parse failure.

§17.280-UX-7 audit-tail concern: ``dag_generator.generate_dag``'s JSON
parse-failure path returned only ``raw_output: gen_result["raw_text"][:500]``.
The operator had to eyeball the truncated snippet for the syntax error
with no line/column/expected-token guidance.

§17.293 adds two pieces:

  1. ``app.utils.llm_parsing.diagnose_json_object_parse(raw)`` — a new
     helper that mirrors ``parse_json_object``'s first cleanup step
     (strip think-tags + markdown fences) then runs ``json.loads`` to
     recover the ``JSONDecodeError`` that ``parse_json_object``
     swallows. Returns ``{lineno, colno, msg, pos}`` on failure, or
     ``None`` when the first parse would have succeeded.

  2. ``dag_generator.generate_dag`` attaches the diagnostic as a
     ``parse_error`` field on the failure dict alongside the existing
     ``raw_output``. The original public API is preserved; the new
     field is additive.

These tests pin both layers.
"""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.utils.llm_parsing import diagnose_json_object_parse


# ---------------------------------------------------------------------------
# Unit tests for the diagnostic helper.
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestDiagnoseJsonObjectParse:
    """§17.293 — the helper that recovers JSONDecodeError context."""

    def test_valid_json_returns_none(self):
        """A well-formed object → no diagnostic to surface."""
        assert diagnose_json_object_parse('{"a": 1}') is None

    def test_returns_lineno_colno_msg(self):
        """A typical malformed object surfaces line + column + message."""
        # Missing comma between keys — a common LLM mistake.
        bad = '{\n  "tasks": []\n  "strategy": "x"\n}'
        diag = diagnose_json_object_parse(bad)
        assert diag is not None
        # Sanity: every expected field is present + correctly typed.
        assert isinstance(diag["lineno"], int) and diag["lineno"] >= 1
        assert isinstance(diag["colno"], int) and diag["colno"] >= 1
        assert isinstance(diag["msg"], str) and diag["msg"]
        assert isinstance(diag["pos"], int) and diag["pos"] >= 0

    def test_diagnostic_points_at_actual_error(self):
        """The reported lineno/colno should point at the missing comma
        (line 3 here)."""
        bad = '{\n  "a": 1\n  "b": 2\n}'
        diag = diagnose_json_object_parse(bad)
        assert diag is not None
        # The missing comma is between line 2 and line 3. JSON parsers
        # typically report the next unexpected token on line 3.
        assert diag["lineno"] == 3

    def test_strips_think_tags_before_parsing(self):
        """Mirrors parse_json_object's first cleanup step — a <think>
        block wrapped around valid JSON must not trip the diagnostic."""
        raw = "<think>reasoning</think>\n{\n  \"a\": 1\n}"
        # The think tag strips → leaves valid JSON → no diagnostic.
        assert diagnose_json_object_parse(raw) is None

    def test_strips_markdown_fences_before_parsing(self):
        """Same first-cleanup step — markdown ```json fences shouldn't
        trip the diagnostic when the contents are valid."""
        raw = "```json\n{\"a\": 1}\n```"
        assert diagnose_json_object_parse(raw) is None

    def test_truncated_json_reports_unexpected_end(self):
        """LLMs often truncate mid-stream — the diagnostic should still
        produce something meaningful (msg about unexpected EOF)."""
        bad = '{"tasks": [{"id": "T1", '  # cut off mid-object
        diag = diagnose_json_object_parse(bad)
        assert diag is not None
        # JSON parsers typically say "Expecting ..." or "Unterminated".
        assert diag["msg"], "msg must be non-empty for a truncated stream"

    def test_not_an_object_returns_none(self):
        """A valid JSON ARRAY parses cleanly with json.loads but isn't a
        dict. The helper returns None — it's not a JSONDecodeError,
        and reporting a fake one would lie about the failure mode.
        Callers should already know parse_json_object returned None
        for shape reasons; the diagnostic is specifically for syntax
        errors."""
        assert diagnose_json_object_parse('[1, 2, 3]') is None


# ---------------------------------------------------------------------------
# End-to-end test against generate_dag's parse-failure path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGenerateDagParseErrorField:
    """§17.293 — generate_dag attaches `parse_error` on JSON-parse failure."""

    async def test_parse_failure_returns_parse_error_field(self):
        """When _generate_dag_with_validator reports "LLM output was not
        valid JSON", the returned dict carries a `parse_error` dict with
        line/col/msg/pos extracted from the original LLM output.

        Pre-§17.293 this dict had only `raw_output` (truncated to 500
        chars) — the operator had to scan the snippet by eye to find
        the syntax error."""
        from app.modules import dag_generator

        bad_raw = '{\n  "tasks": []\n  "strategy": "x"\n}'  # missing comma

        # Mock _generate_dag_with_validator to return the parse-failure
        # shape exactly as the real implementation would.
        async def _stub(*args, **kwargs):
            return {
                "dag_data": None,
                "raw_text": bad_raw,
                "model": "test-model",
                "duration_ms": 100,
                "warnings": [],
                "error": "LLM output was not valid JSON",
                "attempts": 1,
                "validator_calls": 0,
            }

        # Mock the DB-dependent pieces: job lookup + _fail_job.
        # The lookup returns a 5-tuple (status, brief, stored_hash, research_data,
        # node_count) via `result.first()` — research_data was added to the SELECT
        # and unpack; this mock had a stale 4-tuple. We use planning status + a
        # brief + no existing nodes so we land at the LLM call cleanly.
        job_result = MagicMock()
        job_result.first.return_value = (
            "planning", {"title": "test"}, None, None, 0,
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=job_result)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        job_id = str(uuid4())
        with patch.object(dag_generator, "_generate_dag_with_validator", _stub), \
             patch.object(dag_generator, "_fail_job", new_callable=AsyncMock):
            result = await dag_generator.generate_dag(job_id, db)

        assert result["status"] == "failed"
        assert result["error"] == "LLM output was not valid JSON"
        # §17.293 — the new field.
        assert "parse_error" in result
        diag = result["parse_error"]
        assert diag is not None
        assert isinstance(diag["lineno"], int)
        assert isinstance(diag["colno"], int)
        assert diag["msg"]
        # raw_output preserved (backward compat).
        assert "tasks" in result["raw_output"]


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.293 — anchor source so a drive-by refactor that drops the
    diagnostic call (or the public `parse_error` field) shows up here."""

    def test_dag_generator_imports_and_uses_helper(self):
        from app.modules import dag_generator

        with open(dag_generator.__file__, encoding="utf-8") as f:
            src = f.read()

        assert "diagnose_json_object_parse" in src, (
            "§17.293 regression: `diagnose_json_object_parse` import or "
            "call removed from dag_generator.py. The parse-failure path "
            "must surface JSONDecodeError context — pre-§17.293 the "
            "operator saw only a truncated raw snippet."
        )
        assert '"parse_error":' in src, (
            "§17.293 regression: the `parse_error` field is no longer "
            "in the parse-failure return dict. Callers depend on it for "
            "operator-facing diagnostics."
        )

    def test_diagnose_helper_anchored_in_llm_parsing(self):
        from app.utils import llm_parsing

        with open(llm_parsing.__file__, encoding="utf-8") as f:
            src = f.read()

        assert "def diagnose_json_object_parse" in src, (
            "§17.293 regression: the diagnose helper has been removed "
            "from app/utils/llm_parsing.py. Restore it or re-route the "
            "dag_generator call through an equivalent helper."
        )
