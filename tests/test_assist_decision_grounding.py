"""§17.771 (Phase 3) — decision-node commitment grounding.

Pins that (A) the option-research pre-pass threads the operator's system into the
query-generation prompt (system-specific options, not generic), and (B) the
commit deliberation threads fresh research into its prompt (evidence-backed
recommendation, not stale memory). The model is mocked — we assert the CONTEXT
reaches the prompt, not the LLM output.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_guide


def _tool_resp(args: dict):
    call = MagicMock()
    call.arguments = args
    resp = MagicMock()
    resp.success = True
    resp.tool_calls = [call]
    return resp


@pytest.mark.asyncio
async def test_detect_unknowns_threads_environment_into_prompt():
    captured = {}

    async def fake_tool_call(messages, tools, **kw):
        captured["user"] = messages[-1]["content"]
        captured["system"] = messages[0]["content"]
        return _tool_resp({"queries": ["q"]})

    with patch.object(assist_guide.model_router, "tool_call", new=fake_tool_call):
        await assist_guide._detect_unknowns(
            task_text="install the driver", tool="shell", role="model_general",
            max_queries=3, environment_block="RTX 3060, Proxmox 8.2, kernel 6.8")
    assert "RTX 3060" in captured["user"]
    assert "Proxmox 8.2" in captured["user"]
    # the system prompt tells the model to make queries system-specific
    assert "SPECIFIC" in captured["system"]


@pytest.mark.asyncio
async def test_detect_unknowns_no_env_omits_the_block():
    captured = {}

    async def fake_tool_call(messages, tools, **kw):
        captured["user"] = messages[-1]["content"]
        return _tool_resp({"queries": []})

    with patch.object(assist_guide.model_router, "tool_call", new=fake_tool_call):
        await assist_guide._detect_unknowns(
            task_text="install the driver", tool="shell", role="model_general",
            max_queries=3)
    assert "ACTUAL system" not in captured["user"]  # block omitted when no env


@pytest.mark.asyncio
async def test_deliberate_decision_threads_research_into_prompt():
    captured = {}

    async def fake_tool_call(messages, tools, **kw):
        captured["user"] = messages[-1]["content"]
        return _tool_resp({"status": "needs_input", "message": "I'd go with X",
                           "decision_record": ""})

    with patch.object(assist_guide.model_router, "tool_call", new=fake_tool_call):
        res = await assist_guide.deliberate_decision(
            title="Choose driver", task_prompt="pick the driver",
            environment={"profile": "RTX 3060"}, latest_message="which?",
            kind="decision",
            research_block="## Research\n[1] driver 550 is current production")
    assert "driver 550 is current production" in captured["user"]
    assert res["status"] == "needs_input"


@pytest.mark.asyncio
async def test_deliberate_decision_empty_research_is_noop():
    captured = {}

    async def fake_tool_call(messages, tools, **kw):
        captured["user"] = messages[-1]["content"]
        return _tool_resp({"status": "resolved", "message": "", "decision_record": "X"})

    with patch.object(assist_guide.model_router, "tool_call", new=fake_tool_call):
        await assist_guide.deliberate_decision(
            title="t", task_prompt="p", latest_message="go with X",
            kind="decision", research_block="")
    assert "## Research" not in captured["user"]  # nothing injected when empty
