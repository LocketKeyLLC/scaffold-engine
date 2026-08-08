"""§17.688 — a DECISION node is judged on the CHOICE, not against its
downstream concrete-artifact task text.

Reported bug: on an OPNsense assist run, T2 "Define VLAN plan" (a `decision`
node whose task text says "Produce a concrete table: VLAN ID, subnet/CIDR,
DHCP scope, isolation rules") was answered "3 vlans" — exactly the framed
question. The submit-time success verifier judged that against the full-table
task text and returned `failed` → the user saw "⚠️ This may have failed." The
divergence detector flagged the same mismatch. Both now branch on the node
being a decision: the concrete artifact is applied by later implementer steps
(T12 "Create VLAN interfaces", T17 "Configure switch"), so a clear on-topic
choice is a SUCCESS, not a failure/divergence.

The model is always mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_guide, assist_replan


def _toolresp(args: dict, success: bool = True):
    r = MagicMock()
    r.success = success
    call = MagicMock()
    call.arguments = args
    r.tool_calls = [call] if success else []
    r.text = ""
    return r


# ── success verifier: decision-aware ───────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_decision_uses_decision_system_and_passes_choice():
    captured = {}

    async def _capture(messages, tools, **kw):
        captured["system"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        # A clear choice → the (decision-aware) judge returns succeeded.
        return _toolresp({"outcome": "succeeded", "reason": "picked 3 VLANs"})

    with patch.object(assist_guide.model_router, "tool_call", new=_capture):
        v = await assist_guide.verify_step_success(
            title="Define VLAN plan",
            task_prompt="Produce a concrete table: VLAN ID, name, subnet/CIDR, DHCP scope, isolation rules.",
            tool="LLM", evidence="3 vlans", is_decision=True,
        )
    assert v["outcome"] == "succeeded"
    # decision system prompt + "context, not a checklist" framing were used
    assert captured["system"] == assist_guide._VERIFY_DECISION_SYSTEM
    assert "NOT a checklist" in captured["user"]
    assert "later steps apply" in captured["user"]


@pytest.mark.asyncio
async def test_verify_non_decision_keeps_default_system():
    captured = {}

    async def _capture(messages, tools, **kw):
        captured["system"] = messages[0]["content"]
        return _toolresp({"outcome": "succeeded", "reason": "ok"})

    with patch.object(assist_guide.model_router, "tool_call", new=_capture):
        await assist_guide.verify_step_success(
            title="Run migration", task_prompt="Apply the DB migration.",
            tool="shell", evidence="ALTER TABLE ... OK", is_decision=False,
        )
    assert captured["system"] != assist_guide._VERIFY_DECISION_SYSTEM
    # §17.731 — non-decision verify judges against the step's GOAL.
    assert "achieved ITS GOAL" in captured["system"]


@pytest.mark.asyncio
async def test_verify_decision_skips_sandbox_even_for_codegen_tool():
    # A decision node must never route pasted text through the codegen sandbox,
    # even if its tool were mislabeled 'codegen'. If it did, _sandbox_codegen_check
    # would be called; assert it is not.
    with patch.object(assist_guide, "_sandbox_codegen_check",
                      new=AsyncMock(return_value={"verdict": "fail", "reason": "x"})) as sb, \
         patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_toolresp({"outcome": "succeeded", "reason": "ok"}))):
        v = await assist_guide.verify_step_success(
            title="Decide", task_prompt="Choose an option.",
            tool="codegen", evidence="option a", is_decision=True,
        )
    sb.assert_not_called()
    assert v["outcome"] == "succeeded"


# ── divergence detector: decision-aware ────────────────────────────────────


@pytest.mark.asyncio
async def test_divergence_decision_uses_decision_template():
    captured = {}

    async def _capture(messages, tools, **kw):
        captured["user"] = messages[0]["content"]
        return _toolresp({"diverges": False, "severity": "minor", "reason": "clear choice"})

    with patch("app.model_router.tool_call", new=_capture):
        div = await assist_replan.detect_divergence(
            title="Define VLAN plan",
            prompt="Produce a concrete table: VLAN ID, subnet/CIDR, DHCP scope.",
            evidence="3 vlans", is_decision=True,
        )
    assert div["diverges"] is False
    # the decision template (not the default) was formatted
    assert "DECISION STEP TITLE" in captured["user"]
    assert "NOT required in this answer" in captured["user"]
    assert "TASK TITLE:" not in captured["user"]


@pytest.mark.asyncio
async def test_divergence_non_decision_uses_default_template():
    captured = {}

    async def _capture(messages, tools, **kw):
        captured["user"] = messages[0]["content"]
        return _toolresp({"diverges": True, "severity": "major", "reason": "wrong deliverable"})

    with patch("app.model_router.tool_call", new=_capture):
        div = await assist_replan.detect_divergence(
            title="Write the report", prompt="Produce a 500-word report.",
            evidence="def foo(): pass", is_decision=False,
        )
    assert div["diverges"] is True
    assert "TASK TITLE:" in captured["user"]
    assert "DECISION STEP TITLE" not in captured["user"]
