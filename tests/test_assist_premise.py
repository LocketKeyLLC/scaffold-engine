"""§17.864 — step-premise verification unit tests.

The check runs on step claim (GET /assist/{sid}/next): one model call judges
the step against the current facts ledger; a stale verdict stages a §17.677
revision proposal unless one is already pending. Everything is fail-soft — a
flaky check must never block claiming a step.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_premise

pytestmark = pytest.mark.asyncio

_SID = "11111111-2222-3333-4444-555555555555"
_STEP = {"node_key": "T8", "title": "Place the switch",
         "description": "Put the HP switch between modem and router."}


def _db_with_session(facts):
    """A stand-in db whose execute() yields one assist_sessions row."""
    row = {"job_id": "job-1", "status": "active",
           "metadata": {"environment": {"facts": facts}}}
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _model_resp(args):
    resp = MagicMock()
    resp.success = True
    call = MagicMock()
    call.arguments = args
    resp.tool_calls = [call]
    return resp


def _patches(**kw):
    """Common patch set: recap block empty, model verdict, staging seams."""
    return (
        patch.object(assist_premise.settings, "assist_step_premise_check_enabled", True),
        patch("app.modules.assist_agent._note_impact_project_block",
              new=AsyncMock(return_value="")),
        patch.object(assist_premise.model_router, "tool_call",
                     new=AsyncMock(return_value=_model_resp(kw.get("verdict", {"stale": False})))),
        patch("app.modules.assist_notes.get_pending_replan",
              new=AsyncMock(return_value=kw.get("pending", None))),
        patch("app.modules.assist_notes._stage_replan_proposal",
              new=AsyncMock(return_value=kw.get("staged", {"proposals": []}))),
    )


async def test_valve_off_returns_none():
    with patch.object(assist_premise.settings, "assist_step_premise_check_enabled", False):
        out = await assist_premise.check_step_premise(
            session_id=_SID, step=_STEP, db=MagicMock())
    assert out is None


async def test_no_facts_returns_none():
    p = _patches()
    with p[0], p[1], p[2], p[3], p[4]:
        out = await assist_premise.check_step_premise(
            session_id=_SID, step=_STEP, db=_db_with_session([]))
    assert out is None


async def test_consistent_step_reports_not_stale():
    p = _patches(verdict={"stale": False})
    with p[0], p[1], p[2], p[3], p[4]:
        out = await assist_premise.check_step_premise(
            session_id=_SID, step=_STEP, db=_db_with_session(["The router is a SAX1V1K."]))
    assert out == {"stale": False}


async def test_stale_step_stages_proposal():
    stage = AsyncMock(return_value={"proposals": [{"node_key": "T8"}]})
    p = _patches(verdict={"stale": True, "reason": "VLANs abandoned",
                          "current_assumption": "switch inline",
                          "proposed_change": "drop the step"})
    with p[0], p[1], p[2], p[3], \
         patch("app.modules.assist_notes._stage_replan_proposal", new=stage):
        out = await assist_premise.check_step_premise(
            session_id=_SID, step=_STEP,
            db=_db_with_session(["VLAN segmentation is abandoned."]))
    assert out["stale"] is True and out["staged"] is True
    assert out["reason"] == "VLANs abandoned"
    stage.assert_awaited_once()
    kwargs = stage.await_args.kwargs
    assert kwargs["note_kind"] == "premise"
    assert kwargs["affected"][0]["node_key"] == "T8"


async def test_stale_with_pending_proposal_does_not_clobber():
    stage = AsyncMock()
    p = _patches(verdict={"stale": True, "reason": "r", "proposed_change": "c"},
                 pending={"proposals": [{"node_key": "T9"}]})
    with p[0], p[1], p[2], p[3], \
         patch("app.modules.assist_notes._stage_replan_proposal", new=stage):
        out = await assist_premise.check_step_premise(
            session_id=_SID, step=_STEP, db=_db_with_session(["f"]))
    assert out["stale"] is True and out["staged"] is False
    stage.assert_not_awaited()


async def test_model_error_is_fail_soft_none():
    p = _patches()
    with p[0], p[1], p[3], p[4], \
         patch.object(assist_premise.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        out = await assist_premise.check_step_premise(
            session_id=_SID, step=_STEP, db=_db_with_session(["f"]))
    assert out is None


async def test_no_node_key_returns_none():
    with patch.object(assist_premise.settings, "assist_step_premise_check_enabled", True):
        out = await assist_premise.check_step_premise(
            session_id=_SID, step={}, db=MagicMock())
    assert out is None
