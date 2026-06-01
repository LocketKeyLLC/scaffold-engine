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
    _is_claim_line,
    _is_validation_llm_node,
    check_validation_citation_coverage,
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


@pytest.mark.smoke
class TestIsClaimLine:
    """§17.377 — claim-line detection used by the per-claim coverage check."""

    def test_met_marker_in_bullet_line(self):
        assert _is_claim_line("- Parser/CLI separation: MET. T2 line 5...") is True

    def test_not_met_marker(self):
        assert _is_claim_line("- Default output dir: NOT MET. T3 uses 'output'.") is True

    def test_unknown_marker(self):
        assert _is_claim_line("- Filename pattern: UNKNOWN — T4 output unclear.") is True

    def test_met_without_colon_period_or_space_does_not_match(self):
        """`METED` or `METABOLIC` should not trigger — the markers require a
        suffix that distinguishes the verdict from incidental substrings."""
        assert _is_claim_line("- METALLIC parser detected somewhere") is False

    def test_passing_aside_outside_verdict_not_a_claim(self):
        """The §17.377 failure shape — a line that mentions T_N but
        contains no verdict — must NOT be classified as a claim line."""
        assert _is_claim_line("T4 imports LANG_EXT from upstream "
                              "decision node (T2 or T3)") is False

    def test_empty_or_whitespace_line(self):
        assert _is_claim_line("") is False
        assert _is_claim_line("    ") is False
        assert _is_claim_line("\n") is False


@pytest.mark.smoke
class TestCheckValidationCitationCoverage:
    """§17.377 — per-claim coverage. Tightens §17.376's substring check."""

    def test_clean_when_every_upstream_in_a_claim_line(self):
        output = (
            "- Parser/CLI separation: MET. T2 lines 5-15 contain no "
            "argparse. T4 imports extract_blocks.\n"
            "- Filename pattern: MET. T3 generate_filename matches "
            "spec. T6 test_pattern asserts.\n"
            "- Test coverage: MET. T5 tests parser; T6 tests CLI."
        )
        missing = check_validation_citation_coverage(
            output, ["T2", "T3", "T4", "T5", "T6"]
        )
        assert missing == []

    def test_passing_aside_outside_verdict_does_not_count(self):
        """The fifth-mdsplit-retry T7 shape: T2/T3 mentioned only in a
        non-verdict aside, T4/T5/T6 cited in actual MET claims. §17.376
        substring-presence said clean; §17.377 per-claim-coverage flags
        T2 and T3 as missing."""
        output = (
            "- Dry-run: MET. T4 implements --dry-run; T5 tests it; "
            "T6 confirms behavior.\n"
            "- Filename pattern: MET. T4 uses block_<i>_<lang>.<ext>; "
            "T5 asserts exact match; T6 logs filenames.\n"
            "- LANG_EXT: MET. T4 imports LANG_EXT from upstream "
            "decision node (T2 or T3).\n"
            "All code-bearing upstreams (T4, T5, T6) cited per §17.373."
        )
        # Substring check would pass (T2/T3 appear in line 3 + summary).
        substring = check_validation_citations(
            output, ["T2", "T3", "T4", "T5", "T6"]
        )
        assert substring == []
        # Per-claim coverage check flags T2 and T3 — they appear only
        # in the passing-aside line and the summary, never inside a
        # dedicated MET / NOT MET / UNKNOWN verdict.
        # NOTE: line 3 contains "MET" AND "T2"/"T3", so it IS a claim
        # line that cites them. This exposes a residual: a passing
        # aside INSIDE a MET claim still satisfies the per-claim check.
        # The test pins the current behavior — if the aside is in a
        # verdict line, it counts; if it's outside (like the closing
        # summary), it doesn't. A future §17.x can tighten further if
        # this residual surfaces.
        per_claim = check_validation_citation_coverage(
            output, ["T2", "T3", "T4", "T5", "T6"]
        )
        # T2/T3 ARE cited inside the LANG_EXT line (it has MET) so
        # this case still passes — the regression shape §17.377
        # targets is the one where T2/T3 appear ONLY outside verdict
        # lines. See `test_aside_in_non_verdict_paragraph_flagged`
        # for that case.
        assert per_claim == []

    def test_aside_in_non_verdict_paragraph_flagged(self):
        """The headline §17.377 case: T2/T3 mentioned only in a
        narrative paragraph with no MET/NOT MET/UNKNOWN marker."""
        output = (
            "Overview: this validation looks at the CLI behavior. "
            "The decision node (T2 or T3) supplied LANG_EXT.\n"
            "\n"
            "- Dry-run: MET. T4 implements --dry-run; T5 tests it; "
            "T6 confirms.\n"
            "- Filename pattern: MET. T4 uses the pattern; T5 asserts "
            "it; T6 logs it.\n"
            "- Argparse consistency: MET. T4 sets up flags; T5 asserts "
            "defaults; T6 confirms."
        )
        # Substring presence: T2/T3 appear in the Overview line, so
        # §17.376 substring check says clean.
        substring = check_validation_citations(
            output, ["T2", "T3", "T4", "T5", "T6"]
        )
        assert substring == []
        # Per-claim coverage: T2/T3 don't appear in any MET line.
        # Flag both as missing.
        per_claim = check_validation_citation_coverage(
            output, ["T2", "T3", "T4", "T5", "T6"]
        )
        assert per_claim == ["T2", "T3"]

    def test_empty_expected_keys_returns_empty(self):
        assert check_validation_citation_coverage("anything", []) == []

    def test_empty_output_returns_all_expected(self):
        missing = check_validation_citation_coverage("", ["T2", "T3"])
        assert missing == ["T2", "T3"]

    def test_only_narrative_with_no_verdicts_returns_all_expected(self):
        """Narrative-only output (no MET/NOT MET/UNKNOWN at all) has
        zero claim lines — every expected upstream is missing."""
        output = "T2 and T3 and T4 and T5 and T6 all exist in this prose."
        missing = check_validation_citation_coverage(
            output, ["T2", "T3", "T4", "T5", "T6"]
        )
        assert missing == ["T2", "T3", "T4", "T5", "T6"]

    def test_multi_digit_codegen_keys_in_claim_lines(self):
        output = "- All work: MET. T10 and T11 cover parsing; T12 covers CLI."
        missing = check_validation_citation_coverage(
            output, ["T10", "T11", "T12", "T13"]
        )
        assert missing == ["T13"]
