"""§17.628 — top-level natural-language command classification.

`command_guide.classify_command` is the engine-wide sibling of
`assist_guide.classify_turn`: it maps a plain top-level message (no active
assist session) to a READ-ONLY engine intent so the pipeline can drive the
right component by talking.

Pins:
  * a successful tool call returns the model's intent + confidence + slots;
  * the read-only intent surface is exactly the Phase-1 set (no mutating verbs
    leak in — a regression here would let a misfire write/delete);
  * fail-soft: any model error / no-tool-call / garbage intent / empty message
    degrades to intent='none' (confidence 'low') so the caller falls through to
    triage instead of misfiring a command.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import command_guide


def _resp(args: dict | None, success: bool = True):
    r = MagicMock()
    r.success = success
    if success and args is not None:
        call = MagicMock()
        call.arguments = args
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    return r


async def _classify(args, *, success=True, message="hello"):
    with patch.object(command_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_resp(args, success))):
        return await command_guide.classify_command(message=message)


# ── intent surface is read-only + Phase-1 exact ───────────────────────────


@pytest.mark.smoke
def test_intent_surface_is_read_only_phase1():
    # No mutating/expensive verbs (research/schedule/delete/set) may appear —
    # Phase 1 is reads only; those land later behind confirms.
    assert set(command_guide.COMMAND_INTENTS) == {
        "status", "results", "rag_query", "jobs_list", "jobs_find",
        "model_list", "model_available", "model_probe", "help", "none",
    }
    for banned in ("delete", "research", "schedule", "set", "cancel", "confirm"):
        assert not any(banned in i for i in command_guide.COMMAND_INTENTS), banned


@pytest.mark.smoke
def test_route_tool_requires_intent_and_confidence():
    schema = command_guide._ROUTE_TOOL.input_schema
    assert schema["required"] == ["intent", "confidence"]
    assert set(schema["properties"]["intent"]["enum"]) == set(command_guide.COMMAND_INTENTS)


# ── happy paths — intent + slots ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_high_confidence():
    out = await _classify({"intent": "status", "confidence": "high"})
    assert out["intent"] == "status"
    assert out["confidence"] == "high"


@pytest.mark.asyncio
async def test_rag_query_carries_query_slot():
    out = await _classify({"intent": "rag_query", "confidence": "high",
                           "query": "ZFS on non-ECC RAM"})
    assert out["intent"] == "rag_query"
    assert out["query"] == "ZFS on non-ECC RAM"


@pytest.mark.asyncio
async def test_results_carries_job_ref_slot():
    out = await _classify({"intent": "results", "confidence": "high",
                           "job_ref": "proxmox"})
    assert out["intent"] == "results"
    assert out["job_ref"] == "proxmox"


@pytest.mark.asyncio
async def test_slots_are_stripped():
    out = await _classify({"intent": "jobs_find", "confidence": "high",
                           "query": "  kubernetes  "})
    assert out["query"] == "kubernetes"


# ── fail-soft → none ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_message_is_none_without_model_call():
    # Empty/whitespace short-circuits BEFORE any model call.
    with patch.object(command_guide.model_router, "tool_call",
                      new=AsyncMock()) as tc:
        out = await command_guide.classify_command(message="   ")
    assert out["intent"] == "none"
    tc.assert_not_called()


@pytest.mark.asyncio
async def test_model_exception_is_none():
    with patch.object(command_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("ollama down"))):
        out = await command_guide.classify_command(message="what's running")
    assert out["intent"] == "none"
    assert out["confidence"] == "low"


@pytest.mark.asyncio
async def test_no_tool_call_is_none():
    out = await _classify(None, success=True)  # success but empty tool_calls
    assert out["intent"] == "none"


@pytest.mark.asyncio
async def test_unsuccessful_response_is_none():
    out = await _classify({"intent": "status", "confidence": "high"}, success=False)
    assert out["intent"] == "none"


@pytest.mark.asyncio
async def test_garbage_intent_is_none():
    out = await _classify({"intent": "launch_missiles", "confidence": "high"})
    assert out["intent"] == "none"


@pytest.mark.asyncio
async def test_invalid_confidence_coerced_to_low():
    out = await _classify({"intent": "status", "confidence": "banana"})
    assert out["intent"] == "status"
    assert out["confidence"] == "low"
