"""§17.376 — validation-citation guard.

Four mdsplit retries showed the prompt-layer rule (§17.366 → §17.368 →
§17.373) plateaued at "cite the last 3 upstreams" — T2 and T3 stayed
uncited even with §17.373's mechanical "scan the report" instruction.
§17.376 moves the check from prompt-time to verify-time: scan the
validation output for T_N tokens, compare to the code-bearing upstream
set, downgrade verify_status to fail if any are missing.

These tests cover:

  * _is_validation_llm_node — trigger detection (node_type='checkpoint'
    OR title keyword, AND tool='LLM')
  * check_validation_citations — citation-set comparison
  * Empty inputs / edge cases that should NOT fail open silently
"""
from __future__ import annotations

import pytest

from app.modules.execution_verify import (
    _is_validation_llm_node,
    check_validation_citations,
)


@pytest.mark.smoke
class TestIsValidationLLMNode:
    """Trigger detection — when does the guard activate?"""

    def test_checkpoint_node_type_llm_tool_triggers(self):
        assert _is_validation_llm_node("checkpoint", "LLM", "Validate end-to-end") is True

    def test_title_validate_kw_triggers_without_checkpoint(self):
        """Hand-edited row where node_type wasn't set to checkpoint
        but title clearly says Validate — guard still applies."""
        assert _is_validation_llm_node("task", "LLM", "Validate the spec") is True

    def test_title_verify_kw_triggers(self):
        assert _is_validation_llm_node("task", "LLM", "Verify telemetry-free") is True

    def test_title_check_kw_triggers(self):
        assert _is_validation_llm_node("task", "LLM", "Check compliance") is True

    def test_title_audit_kw_triggers(self):
        assert _is_validation_llm_node("task", "LLM", "Audit the deployment") is True

    def test_non_llm_tool_never_triggers(self):
        """CodeGen / Shell / Milvus / SearXNG nodes are never
        validation candidates — the guard is LLM-only."""
        assert _is_validation_llm_node("checkpoint", "CodeGen", "Validate parser") is False
        assert _is_validation_llm_node("checkpoint", "Shell", "Verify install") is False
        assert _is_validation_llm_node("checkpoint", "Milvus", "Check KB") is False
        assert _is_validation_llm_node("checkpoint", "SearXNG", "Audit web") is False

    def test_non_validation_title_does_not_trigger(self):
        assert _is_validation_llm_node("task", "LLM", "Document the setup") is False
        assert _is_validation_llm_node("task", "LLM", "Design the parser") is False

    def test_case_insensitive_tool_match(self):
        # The clause's tool comparison is case-insensitive — matches
        # the existing tool_lower convention in execute_next_node.
        assert _is_validation_llm_node("checkpoint", "llm", "Validate") is True
        assert _is_validation_llm_node("checkpoint", "LLM", "Validate") is True
        assert _is_validation_llm_node("checkpoint", "Llm", "Validate") is True

    def test_none_inputs_dont_crash(self):
        """The helper accepts None for all three args and returns False
        — fail-open on missing data."""
        assert _is_validation_llm_node(None, None, None) is False
        assert _is_validation_llm_node(None, "LLM", None) is False
        assert _is_validation_llm_node("checkpoint", None, "Validate") is False


@pytest.mark.smoke
class TestCheckValidationCitations:
    """The actual citation-set comparison."""

    def test_clean_when_all_upstreams_cited(self):
        output = (
            "- Parser/CLI separation: MET. T2 exports extract_blocks. "
            "T3 exports generate_filename. T4 imports both. T5 tests "
            "parser. T6 tests CLI."
        )
        missing = check_validation_citations(output, ["T2", "T3", "T4", "T5", "T6"])
        assert missing == []

    def test_returns_missing_keys_when_some_uncited(self):
        """The §17.371-§17.373 retry shape — T7 cited T4/T5/T6, missed
        T2 and T3. The guard must flag T2 and T3."""
        output = (
            "- Parser/CLI separation: MET. T4 imports T5. "
            "T5 tests pass. T6 tests pass."
        )
        missing = check_validation_citations(output, ["T2", "T3", "T4", "T5", "T6"])
        assert missing == ["T2", "T3"]

    def test_empty_expected_keys_returns_empty(self):
        """If no code-bearing upstreams existed (LLM-only DAG, or all
        upstreams failed), the guard short-circuits."""
        assert check_validation_citations("anything", []) == []

    def test_empty_output_returns_all_expected(self):
        """An empty validation output cites nothing — every expected
        upstream is missing."""
        missing = check_validation_citations("", ["T2", "T3"])
        assert missing == ["T2", "T3"]

    def test_none_output_returns_all_expected(self):
        """Defensive: None output (e.g., validator timed out before
        emitting anything) treated as zero citations."""
        missing = check_validation_citations(None, ["T2", "T3"])  # type: ignore[arg-type]
        assert missing == ["T2", "T3"]

    def test_t_token_must_be_word_bounded(self):
        """`T2` matches; `T2_extension` does not — the regex uses \\b
        word boundaries so partial matches inside identifiers don't
        register as citations."""
        # The output references a variable name `T2_threshold`; that
        # should NOT count as citing T2.
        output = "Some prose. The constant T2_threshold appears here."
        missing = check_validation_citations(output, ["T2"])
        assert missing == ["T2"]

    def test_double_digit_node_keys_supported(self):
        """A DAG with > 9 nodes uses T10, T11, etc. The regex must
        match multi-digit suffixes."""
        output = "T10 exports extract_blocks; T11 exports filename gen."
        missing = check_validation_citations(output, ["T10", "T11", "T12"])
        assert missing == ["T12"]

    def test_duplicate_citations_count_as_one(self):
        """Citing T2 five times still satisfies "at least one"."""
        output = "T2 says X. T2 also says Y. T2 confirms Z. T3 too."
        missing = check_validation_citations(output, ["T2", "T3"])
        assert missing == []
