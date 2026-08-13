"""§17.771 (deferred, now done) — render-path decision suggestion validation.

A DECISION step's first-view walkthrough must carry a recommendation. These pin
the detector and the enforce-on-miss behavior (valve-gated; the follow-up call is
mocked). Non-decision steps and the valve-off default are untouched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_guide
from app.modules.prompt_assembly import StepContext


def _ctx(tool: str = "LLM"):
    return StepContext(
        node_key="D2", title="Choose the media server", tool=tool, domain=None,
        system_prompt="sys", base_prompt="Pick the media server software.",
        upstream_outputs={}, upstream_truncated_keys=[], grounding="",
        grounding_kind=None, assembled_prompt="Pick the media server software.",
    )


def _resp(text: str, success: bool = True):
    r = MagicMock()
    r.success = success
    r.text = text
    r.error = None
    r.model = "fake"
    return r


def _tool_resp(args: dict):
    call = MagicMock()
    call.arguments = args
    r = MagicMock()
    r.success = True
    r.tool_calls = [call]
    return r


_OPTIONS_NO_SUGGESTION = "## The decision\nWhich media server?\n\n## Options\n- Jellyfin — FOSS\n- Plex — polished"


# ── detector ──────────────────────────────────────────────────────────────────

def test_detects_suggestion_heading():
    assert assist_guide._has_decision_suggestion("## My suggestion\nI'd lean X")
    assert assist_guide._has_decision_suggestion("## Recommendation\nGo Y")


def test_detects_lean_phrase_without_heading():
    assert assist_guide._has_decision_suggestion("...honestly I'd go with Jellyfin here")
    assert assist_guide._has_decision_suggestion("I recommend Plex for this")


def test_absent_suggestion_is_false():
    assert not assist_guide._has_decision_suggestion(_OPTIONS_NO_SUGGESTION)


# ── enforcement ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enforce_appends_suggestion_on_miss_when_valve_on():
    with patch.object(settings, "assist_decision_suggestion_enforce", True), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp(_OPTIONS_NO_SUGGESTION))), \
         patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_tool_resp(
                          {"leaning": "Jellyfin", "why": "it's FOSS and fits your box"}))) as tc:
        res = await assist_guide.generate_guidance(
            ctx=_ctx(), research=False, node_key="D2", is_decision=True,
        )
    assert "## My suggestion" in res["guidance"]
    assert "Jellyfin" in res["guidance"]
    assert res["guidance_meta"]["suggestion_enforced"] is True
    tc.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_enforce_when_valve_off():
    # Pin the valve explicitly — the live container runs with the compose env
    # ASSIST_DECISION_SUGGESTION_ENFORCE=true, so relying on the code default here
    # would read the runtime value (the recurring container-valve test gotcha).
    with patch.object(settings, "assist_decision_suggestion_enforce", False), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp(_OPTIONS_NO_SUGGESTION))), \
         patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        res = await assist_guide.generate_guidance(
            ctx=_ctx(), research=False, node_key="D2", is_decision=True,
        )
    assert "## My suggestion" not in res["guidance"]
    assert res["guidance_meta"]["suggestion_enforced"] is False
    tc.assert_not_called()  # default off → no follow-up call


@pytest.mark.asyncio
async def test_no_enforce_when_suggestion_already_present():
    have = _OPTIONS_NO_SUGGESTION + "\n\n## My suggestion\nI'd lean Jellyfin — your call."
    with patch.object(settings, "assist_decision_suggestion_enforce", True), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp(have))), \
         patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        res = await assist_guide.generate_guidance(
            ctx=_ctx(), research=False, node_key="D2", is_decision=True,
        )
    assert res["guidance_meta"]["suggestion_enforced"] is False
    tc.assert_not_called()  # already has one → no follow-up


@pytest.mark.asyncio
async def test_no_enforce_for_non_decision_step():
    with patch.object(settings, "assist_decision_suggestion_enforce", True), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("## Run this\n1. do it"))), \
         patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3", is_decision=False,
        )
    assert res["guidance_meta"]["suggestion_enforced"] is False
    tc.assert_not_called()  # non-decision → enforcement never runs


@pytest.mark.asyncio
async def test_enforce_failsoft_when_followup_returns_no_leaning():
    with patch.object(settings, "assist_decision_suggestion_enforce", True), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp(_OPTIONS_NO_SUGGESTION))), \
         patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_tool_resp({"leaning": "", "why": "x"}))):
        res = await assist_guide.generate_guidance(
            ctx=_ctx(), research=False, node_key="D2", is_decision=True,
        )
    # empty leaning → append is "" → ship the un-enforced walkthrough, still ready
    assert res["status"] == "ready"
    assert res["guidance_meta"]["suggestion_enforced"] is False
    assert "## My suggestion" not in res["guidance"]
