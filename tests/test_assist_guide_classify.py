"""§17.626 — natural-language turn classification (assist_guide.classify_turn).

Pins:
  * the walkthrough system prompts carry the heading-meta rule that stops the
    model echoing the section descriptions as headings (the reported bug);
  * classify_turn returns the model's intent + evidence/error_text;
  * a rejected/failed/garbage tool call fails soft to intent='question' so a
    flaky classifier degrades to guidance, never a misfired submit/skip.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_guide


def _classify_resp(args: dict | None, success: bool = True):
    r = MagicMock()
    r.success = success
    if success and args is not None:
        call = MagicMock()
        call.arguments = args
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    return r


# ── heading-echo guard (the "not understandable" bug) ─────────────────────


@pytest.mark.smoke
@pytest.mark.parametrize("prompt", [
    assist_guide.GUIDE_SYSTEM_CODEGEN,
    assist_guide.GUIDE_SYSTEM_NONCODE,
    assist_guide.GUIDE_SYSTEM_FIX,
])
def test_guide_prompts_forbid_echoing_section_descriptions(prompt):
    # The reported symptom: headings rendered as
    # "## Goal — one or two sentences: what this step produces…" because the
    # description sat on the heading line. The rule must be present, and the
    # heading tokens must appear as clean `## X` lines.
    assert "NEVER copy" in prompt
    assert "not text for the reader" in prompt


@pytest.mark.smoke
def test_noncode_prompt_has_clean_heading_tokens():
    p = assist_guide.GUIDE_SYSTEM_NONCODE
    # Clean heading lines (token only), not "## Goal — <description>".
    for heading in ("## Goal", "## Steps", "## Done when", "## Inputs needed"):
        assert f"\n{heading}\n" in p, heading


# ── classify_turn ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_turn_submit_returns_evidence():
    resp = _classify_resp({"intent": "submit", "evidence": "picked ZFS with VLANs"})
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=resp)):
        out = await assist_guide.classify_turn(
            message="ok done, I picked ZFS with VLANs",
            title="Resolve config", task_prompt="Decide ZFS vs LVM", tool="LLM",
        )
    assert out["intent"] == "submit"
    assert out["evidence"] == "picked ZFS with VLANs"


@pytest.mark.asyncio
async def test_classify_turn_fix_returns_error_text():
    resp = _classify_resp({"intent": "fix", "error_text": "apt: package not found"})
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=resp)):
        out = await assist_guide.classify_turn(
            message="it failed with apt: package not found",
            title="Install", task_prompt="apt install x", tool="shell",
        )
    assert out["intent"] == "fix"
    assert out["error_text"] == "apt: package not found"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    _classify_resp({"intent": "banana"}),          # not in enum
    _classify_resp(None),                          # no tool call
    _classify_resp({"intent": "submit"}, success=False),  # provider failure
])
async def test_classify_turn_fails_soft_to_question(bad):
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=bad)):
        out = await assist_guide.classify_turn(
            message="whatever", title="t", task_prompt="p", tool="LLM",
        )
    assert out["intent"] == "question"


@pytest.mark.asyncio
async def test_classify_turn_swallows_provider_exception():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("ollama down"))):
        out = await assist_guide.classify_turn(
            message="next", title="t", task_prompt="p", tool="LLM",
        )
    assert out["intent"] == "question"


# ── §17.627 — expanded intent surface ─────────────────────────────────────


@pytest.mark.smoke
def test_expanded_intent_enum_covers_components():
    for i in ("handoff", "status", "explain_plan", "set_env",
              "set_verbosity", "ask"):
        assert i in assist_guide.ASSIST_INTENTS


@pytest.mark.asyncio
async def test_classify_turn_ask_returns_query():
    resp = _classify_resp({"intent": "ask",
                           "query": "is ZFS safe without ECC RAM"})
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=resp)):
        out = await assist_guide.classify_turn(
            message="wait is zfs ok without ecc?", title="Storage",
            task_prompt="Decide ZFS vs LVM", tool="LLM",
        )
    assert out["intent"] == "ask"
    assert out["query"] == "is ZFS safe without ECC RAM"


# ── §17.651 — ask vs question boundary (project/off-step → ask) ────────────


@pytest.mark.smoke
def test_classify_prompt_routes_project_questions_to_ask():
    """A design/planning question about the project or a DIFFERENT step must
    classify as ask (→ project-aware research, §17.650), not question (which
    only re-renders the CURRENT step). Guards the prompt wording since the
    boundary itself is model-driven and can't be unit-asserted hermetically."""
    sys_prompt = assist_guide._CLASSIFY_SYSTEM
    tool_desc = assist_guide._CLASSIFY_TURN_TOOL.input_schema[
        "properties"]["intent"]["description"]
    for blob in (sys_prompt, tool_desc):
        low = blob.lower()
        # ask must explicitly cover project/design/planning + other-step questions
        assert "project" in low and "ask" in low
        # question must be scoped to the CURRENT step only
        assert "current step" in low or "this step" in low


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["handoff", "status", "explain_plan", "set_env"])
async def test_classify_turn_passes_through_new_intents(intent):
    resp = _classify_resp({"intent": intent})
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=resp)):
        out = await assist_guide.classify_turn(
            message="whatever", title="t", task_prompt="p", tool="LLM",
        )
    assert out["intent"] == intent
