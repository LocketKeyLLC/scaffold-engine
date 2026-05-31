"""Sprint W.3 — unit tests for dag_validator.

Validates the second-pass tool-pick auditor. Mocks model_router.generate.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.dag_validator import (
    ToolIssue,
    ValidatorOutcome,
    issue_set_signature,
    render_corrections_block,
    validate_tool_picks,
)


def _llm_response(text: str, success: bool = True, error: str | None = None):
    """Build a fake ModelResponse-shaped object for mocking."""
    resp = MagicMock()
    resp.success = success
    resp.text = text
    resp.error = error
    resp.model = "fake-model"
    resp.total_duration_ms = 0
    return resp


@pytest.mark.smoke
class TestValidateToolPicks:
    """validate_tool_picks() behavior under various LLM responses."""

    async def test_clean_dag_returns_no_issues(self):
        tasks = [
            {"id": "T1", "name": "A", "tool": "LLM"},
            {"id": "T2", "name": "B", "tool": "CodeGen"},
        ]
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response('{"issues": []}')),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.error is None
        assert outcome.issues == []

    async def test_codegen_for_documentation_flagged(self):
        tasks = [
            {"id": "T1", "name": "Write parser", "tool": "CodeGen"},
            {"id": "T2", "name": "Document usage", "tool": "CodeGen"},
        ]
        validator_payload = {
            "issues": [
                {
                    "node_id": "T2", "current_tool": "CodeGen",
                    "proposed_tool": "LLM",
                    "reason": "Documentation is not executable code.",
                }
            ]
        }
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response(json.dumps(validator_payload))),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.error is None
        assert len(outcome.issues) == 1
        assert outcome.issues[0].node_id == "T2"
        assert outcome.issues[0].proposed_tool == "LLM"

    async def test_searxng_for_kb_flagged_to_milvus(self):
        tasks = [
            {"id": "T1", "name": "Look up KB", "tool": "SearXNG",
             "notes": "Search the knowledge base for prior decisions"},
        ]
        payload = {
            "issues": [
                {
                    "node_id": "T1", "current_tool": "SearXNG",
                    "proposed_tool": "Milvus",
                    "reason": "KB lookups must use Milvus, not SearXNG.",
                }
            ]
        }
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response(json.dumps(payload))),
        ):
            outcome = await validate_tool_picks(tasks)
        assert len(outcome.issues) == 1
        assert outcome.issues[0].proposed_tool == "Milvus"

    async def test_malformed_json_returns_error_no_issues(self):
        tasks = [{"id": "T1", "name": "X", "tool": "LLM"}]
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response("this is not JSON {{{")),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.issues == []
        assert outcome.error == "json_parse_failed"

    async def test_unsuccessful_response_returns_error(self):
        tasks = [{"id": "T1", "name": "X", "tool": "LLM"}]
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response("", success=False, error="boom")),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.issues == []
        assert outcome.error and "boom" in outcome.error

    async def test_call_exception_returns_error(self):
        tasks = [{"id": "T1", "name": "X", "tool": "LLM"}]
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(side_effect=RuntimeError("network down")),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.issues == []
        assert outcome.error and "network down" in outcome.error

    async def test_schema_mismatch_returns_error(self):
        """Validator returns parseable JSON but no 'issues' key."""
        tasks = [{"id": "T1", "name": "X", "tool": "LLM"}]
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response('{"foo": "bar"}')),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.issues == []
        assert outcome.error == "schema_mismatch"

    async def test_no_op_proposal_filtered(self):
        """A validator suggestion where current_tool == proposed_tool is dropped."""
        tasks = [{"id": "T1", "name": "A", "tool": "LLM"}]
        payload = {
            "issues": [
                {"node_id": "T1", "current_tool": "LLM",
                 "proposed_tool": "LLM", "reason": "no-op"},
            ]
        }
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response(json.dumps(payload))),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.issues == []
        assert outcome.error is None

    async def test_invalid_proposed_tool_filtered(self):
        """If validator proposes a non-canonical tool, drop the suggestion."""
        tasks = [{"id": "T1", "name": "A", "tool": "LLM"}]
        payload = {
            "issues": [
                {"node_id": "T1", "current_tool": "LLM",
                 "proposed_tool": "WebSearch", "reason": "invalid proposal"},
            ]
        }
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response(json.dumps(payload))),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.issues == []
        assert outcome.error is None

    async def test_scope_issue_same_tool_kept_when_reason_says_scope(self):
        """§17.363 — same-tool suggestions (current==proposed) used to be
        silently dropped by the no-op filter, which discarded every scope
        finding because the validator can't propose a different tool for
        scope inflation. The relaxed filter keeps same-tool issues whose
        reason explicitly diagnoses scope."""
        tasks = [{"id": "T2", "name": "Configure VLAN bridges", "tool": "Shell",
                  "notes": "outputs include all 4 LXCs running and tailscale install"}]
        payload = {
            "issues": [
                {
                    "node_id": "T2", "current_tool": "Shell",
                    "proposed_tool": "Shell",
                    "reason": (
                        "Scope inflation: name is 'Configure VLAN bridges' but "
                        "outputs include 'all 4 LXCs running'. Trim outputs to "
                        "bridges only; LXC creation belongs to a downstream node."
                    ),
                }
            ]
        }
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response(json.dumps(payload))),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.error is None
        assert len(outcome.issues) == 1
        assert outcome.issues[0].node_id == "T2"
        # Same tool both sides — the no-op filter must let this through
        # because the reason explicitly says "Scope inflation".
        assert outcome.issues[0].current_tool == "Shell"
        assert outcome.issues[0].proposed_tool == "Shell"
        assert "scope" in outcome.issues[0].reason.lower()

    async def test_same_tool_non_scope_reason_still_dropped(self):
        """Non-scope same-tool suggestions remain no-ops. The relaxation is
        narrow — only `reason ~ "scope"` rescues them; a same-tool issue
        with an unrelated reason (model error, hallucination) still drops."""
        tasks = [{"id": "T1", "name": "Plan", "tool": "LLM"}]
        payload = {
            "issues": [
                {
                    "node_id": "T1", "current_tool": "LLM",
                    "proposed_tool": "LLM",
                    "reason": "no-op suggestion with no scope diagnosis",
                }
            ]
        }
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response(json.dumps(payload))),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.error is None
        assert outcome.issues == []

    async def test_validator_system_documents_scope_audit(self):
        """The VALIDATOR_SYSTEM prompt must instruct the auditor to flag
        scope issues, not just tool issues. Static surface check — without
        it, the validator silently regresses to tool-only auditing."""
        from app.modules.dag_validator import VALIDATOR_SYSTEM
        assert "SCOPE DISCIPLINE" in VALIDATOR_SYSTEM
        assert "scope inflation" in VALIDATOR_SYSTEM.lower()
        assert "proposed_tool = current_tool" in VALIDATOR_SYSTEM

    async def test_validator_system_documents_codegen_scope_audit(self):
        """§17.367 — VALIDATOR_SYSTEM must extend scope audit to CodeGen
        verbs (Write CLI / Implement / Write unit tests), not just Shell."""
        from app.modules.dag_validator import VALIDATOR_SYSTEM
        # The §17.367 update to the SCOPE DISCIPLINE block.
        # Collapse whitespace before substring match so future prompt
        # reflows don't break the test on line wraps.
        flat = " ".join(VALIDATOR_SYSTEM.split())
        assert "CodeGen verbs follow the same rule" in flat
        assert 'Write unit tests for X' in flat
        # The key signal — tests must import, not re-stub.
        assert "tests import; they do not re-stub" in flat

    async def test_llm_for_install_task_flagged_to_shell(self):
        """§17.359 — install/configure verbs on a host must not be LLM.

        Closes the OVERVIEW §17.359 trial regression: 9 nodes all tagged
        `tool=LLM` for `Install Proxmox VE`, `Configure GPU passthrough`,
        etc.; the LLM then narrated past-tense fake execution. The
        validator now flags those as Shell candidates.
        """
        tasks = [
            {"id": "T1", "name": "Install Proxmox VE", "tool": "LLM",
             "notes": "Action on host — installs hypervisor"},
            {"id": "T2", "name": "Configure GPU passthrough", "tool": "LLM",
             "notes": "Edits /etc/modprobe.d, updates initramfs"},
        ]
        payload = {
            "issues": [
                {"node_id": "T1", "current_tool": "LLM",
                 "proposed_tool": "Shell",
                 "reason": "Installing software on a host is a Shell action."},
                {"node_id": "T2", "current_tool": "LLM",
                 "proposed_tool": "Shell",
                 "reason": "Modifying host config files is a Shell action."},
            ]
        }
        with patch(
            "app.modules.dag_validator.model_router.generate",
            new=AsyncMock(return_value=_llm_response(json.dumps(payload))),
        ):
            outcome = await validate_tool_picks(tasks)
        assert outcome.error is None
        assert len(outcome.issues) == 2
        assert {i.proposed_tool for i in outcome.issues} == {"Shell"}

    async def test_empty_tasks_short_circuits_no_llm_call(self):
        """No LLM call when there's nothing to audit."""
        mock = AsyncMock()
        with patch(
            "app.modules.dag_validator.model_router.generate", new=mock,
        ):
            outcome = await validate_tool_picks([])
        assert outcome.issues == []
        assert outcome.error is None
        mock.assert_not_called()


@pytest.mark.smoke
class TestIssueSetSignature:
    """Circuit-breaker signature is stable under reordering and metadata-only changes."""

    def test_same_issue_set_same_signature(self):
        a = [
            ToolIssue("T1", "CodeGen", "LLM", "x"),
            ToolIssue("T2", "SearXNG", "Milvus", "y"),
        ]
        b = [
            ToolIssue("T2", "SearXNG", "Milvus", "different reason"),
            ToolIssue("T1", "CodeGen", "LLM", "different reason"),
        ]
        assert issue_set_signature(a) == issue_set_signature(b)

    def test_different_proposed_tool_diff_signature(self):
        a = [ToolIssue("T1", "CodeGen", "LLM", "x")]
        b = [ToolIssue("T1", "CodeGen", "Milvus", "x")]
        assert issue_set_signature(a) != issue_set_signature(b)

    def test_empty_set_signature_stable(self):
        assert issue_set_signature([]) == ()


@pytest.mark.smoke
class TestRenderCorrectionsBlock:
    def test_block_includes_attempt_number_and_issues(self):
        issues = [
            ToolIssue("T2", "CodeGen", "LLM", "Documentation is not code."),
            ToolIssue("T3", "SearXNG", "Milvus", "KB lookup."),
        ]
        block = render_corrections_block(issues, attempt=2)
        assert "attempt 2" in block
        assert "T2" in block
        assert "T3" in block
        assert "CodeGen" in block
        assert "Documentation is not code." in block
        # Must steer the model to apply corrections, not start fresh
        assert "applying the corrections" in block.lower()
