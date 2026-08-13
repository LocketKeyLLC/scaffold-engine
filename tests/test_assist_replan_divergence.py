"""Audit B4 — direct tests for assist_replan.detect_divergence.

The pre-B4 implementation used model_router.chat() + parse_json_object;
existing tests (test_assist_replan_regen.py) only patched detect_divergence
as a black box. This file covers the new tool_call() path end-to-end:

- happy path: diverges=True / severity=major / reason populated
- happy path: diverges=False (no replan triggered)
- fail-closed: dispatch raises → 'detection_unavailable'
- fail-closed: no tool_calls in response → 'detection_unparsed'
- fail-closed: tool_calls present but missing 'diverges' key → 'detection_unparsed'
- the registered tool schema is RECORD_DIVERGENCE_TOOL, not chat coaxing
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.modules import assist_replan
from app.providers.base import ToolCall


def _resp_with_args(arguments: dict):
    """Build a model_router.tool_call() response with one ToolCall."""
    resp = types.SimpleNamespace()
    resp.text = ""
    resp.tool_calls = [
        ToolCall(id="t0", name="record_divergence", arguments=arguments),
    ]
    resp.success = True
    resp.error = None
    return resp


def _resp_empty():
    resp = types.SimpleNamespace()
    resp.text = ""
    resp.tool_calls = []
    resp.success = True
    resp.error = None
    return resp


@pytest.mark.smoke
class TestDetectDivergence:
    async def test_diverges_major_returns_full_payload(self):
        resp = _resp_with_args({
            "diverges": True,
            "severity": "major",
            "reason": "deliverable type changed from spec to code",
        })
        with patch("app.model_router.tool_call", AsyncMock(return_value=resp)) as mock_tc:
            result = await assist_replan.detect_divergence(
                title="Write the spec",
                prompt="Produce a 1-page spec for the API",
                evidence="def handler(): pass",
            )
        assert result == {
            "diverges": True,
            "severity": "major",
            "reason": "deliverable type changed from spec to code",
        }
        # The wrapper was called with the new tool, not chat()-coaxing.
        kwargs = mock_tc.await_args.kwargs
        assert assist_replan.RECORD_DIVERGENCE_TOOL in kwargs["tools"]

    async def test_diverges_false_passes_through_severity_default(self):
        resp = _resp_with_args({"diverges": False})  # no severity
        with patch("app.model_router.tool_call", AsyncMock(return_value=resp)):
            result = await assist_replan.detect_divergence(
                title="t", prompt="p", evidence="e",
            )
        assert result["diverges"] is False
        assert result["severity"] == "minor"  # default when omitted
        assert result["reason"] == ""

    async def test_dispatch_failure_returns_detection_unavailable(self):
        with patch(
            "app.model_router.tool_call",
            AsyncMock(side_effect=RuntimeError("ollama offline")),
        ):
            result = await assist_replan.detect_divergence(
                title="t", prompt="p", evidence="e",
            )
        assert result == {
            "diverges": False,
            "severity": "minor",
            "reason": "detection_unavailable",
        }

    async def test_no_tool_calls_returns_detection_unparsed(self):
        with patch("app.model_router.tool_call", AsyncMock(return_value=_resp_empty())):
            result = await assist_replan.detect_divergence(
                title="t", prompt="p", evidence="e",
            )
        assert result == {
            "diverges": False,
            "severity": "minor",
            "reason": "detection_unparsed",
        }

    async def test_missing_diverges_key_returns_detection_unparsed(self):
        # Tool was called but the model didn't include the required field.
        # read_tool_args returns the (broken) args dict; our code rejects it.
        resp = _resp_with_args({"severity": "major", "reason": "yes"})
        with patch("app.model_router.tool_call", AsyncMock(return_value=resp)):
            result = await assist_replan.detect_divergence(
                title="t", prompt="p", evidence="e",
            )
        assert result["reason"] == "detection_unparsed"
        assert result["diverges"] is False

    async def test_evidence_and_prompt_truncated_at_4000_chars(self):
        # The function caps evidence + prompt to 4000 chars to keep the
        # prompt under the model's context cap. Verify a 5000-char input
        # is truncated when assembled into the message.
        big_evidence = "X" * 5000
        big_prompt = "Y" * 5000
        resp = _resp_with_args({"diverges": False})
        with patch("app.model_router.tool_call", AsyncMock(return_value=resp)) as mock_tc:
            await assist_replan.detect_divergence(
                title="t", prompt=big_prompt, evidence=big_evidence,
            )
        message_content = mock_tc.await_args.kwargs["messages"][0]["content"]
        # Both X-block and Y-block should be present at exactly 4000 chars each.
        assert "X" * 4000 in message_content
        assert "X" * 4001 not in message_content
        assert "Y" * 4000 in message_content
        assert "Y" * 4001 not in message_content


@pytest.mark.smoke
class TestRecordDivergenceTool:
    """The Tool schema itself is part of the public contract — its shape
    affects every divergence verdict the model returns. Lock the contract."""

    def test_required_fields(self):
        schema = assist_replan.RECORD_DIVERGENCE_TOOL.input_schema
        assert schema["required"] == ["diverges"]

    def test_diverges_is_boolean(self):
        props = assist_replan.RECORD_DIVERGENCE_TOOL.input_schema["properties"]
        assert props["diverges"]["type"] == "boolean"

    def test_severity_enum(self):
        props = assist_replan.RECORD_DIVERGENCE_TOOL.input_schema["properties"]
        assert set(props["severity"]["enum"]) == {"minor", "major"}

    def test_tool_name(self):
        assert assist_replan.RECORD_DIVERGENCE_TOOL.name == "record_divergence"


@pytest.mark.smoke
class TestDispatchPath:
    """§17.89 Pattern 3 — detect_divergence routes through role= so the
    configured MODEL_VERIFIER_PROVIDER is honored. Pre-§17.89 it resolved
    settings.model_verifier itself and bypassed the provider abstraction."""

    async def test_dispatches_via_role_not_model(self):
        resp = _resp_with_args({"diverges": False})
        with patch("app.model_router.tool_call",
                   AsyncMock(return_value=resp)) as mock_tc:
            await assist_replan.detect_divergence(
                title="t", prompt="p", evidence="e",
            )
        kwargs = mock_tc.await_args.kwargs
        # §17.771 (Phase 0) — the role is now settings-driven (was hardcoded
        # "model_verifier"/kimi; default flipped to model_general because kimi
        # false-negates on assist semantics). Track the setting so a future
        # re-tune doesn't re-break this; the point of the test is dispatch via
        # role=, NOT model=.
        assert kwargs.get("role") == settings.assist_divergence_model_role
        assert "model" not in kwargs

    async def test_model_overrides_flow_through_to_provider(self):
        """Per-request overrides forward to model_router.tool_call as the
        ``overrides`` kwarg so provider_for_role's override precedence works."""
        resp = _resp_with_args({"diverges": False})
        overrides = {"model_verifier": "qwen3:7b"}
        with patch("app.model_router.tool_call",
                   AsyncMock(return_value=resp)) as mock_tc:
            await assist_replan.detect_divergence(
                title="t", prompt="p", evidence="e",
                model_overrides=overrides,
            )
        kwargs = mock_tc.await_args.kwargs
        assert kwargs.get("overrides") == overrides
