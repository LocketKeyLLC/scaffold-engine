"""§17.771 — adapt-the-plan on a goal-met-via-alternative commit.

When a step is done via a valid alternative because a hardware/software
constraint ruled out the planned method, the system RECOGNIZES it (verifier
signal), records the constraint durably, and re-plans the pending steps that
assumed the impossible method — instead of blocking, skipping, or rubber-stamping.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_agent, assist_guide, assist_replan


def _tool_resp(args: dict):
    call = MagicMock()
    call.arguments = args
    r = MagicMock()
    r.success = True
    r.tool_calls = [call]
    return r


# ── verifier emits the constraint-adaptation signal ───────────────────────────

@pytest.mark.asyncio
async def test_verifier_emits_goal_met_via_alternative_and_constraint():
    args = {"outcome": "succeeded", "reason": "goal met via automatic control",
            "goal_met_via_alternative": True,
            "constraint": "NCT7904D locks PWM to automatic; manual curves impossible"}
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_tool_resp(args))):
        r = await assist_guide.verify_step_success(
            title="fan control", task_prompt="manual PWM via fancontrol",
            tool="LLM", evidence="chip locks pwm to auto; temps < 70C")
    assert r["outcome"] == "succeeded"
    assert r["goal_met_via_alternative"] is True
    assert "NCT7904D" in r["constraint"]


@pytest.mark.asyncio
async def test_via_alternative_cleared_when_not_succeeded():
    """The signal only counts on a 'succeeded' outcome (guards against a model
    setting the flag on incomplete/failed)."""
    args = {"outcome": "incomplete", "reason": "only setup",
            "goal_met_via_alternative": True, "constraint": "something"}
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_tool_resp(args))):
        r = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="LLM", evidence="e")
    assert r["goal_met_via_alternative"] is False
    assert r["constraint"] == ""


# ── adapt_step_to_constraint: record + re-plan downstream ──────────────────────

def _db_with_session(status="active"):
    row = {"job_id": "J1", "status": status, "metadata": {}}
    res = MagicMock()
    res.mappings.return_value.first.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=res)
    return db


@pytest.mark.asyncio
async def test_adapt_records_note_and_stages_downstream_replan():
    affected = [{"node_key": "A2", "action": "revise",
                 "current_assumption": "custom fan curve", "proposed_change": "automatic control"}]
    with patch.object(settings, "assist_note_replan_enabled", True), \
         patch.object(assist_agent, "record_note", new=AsyncMock()) as note, \
         patch.object(assist_agent, "_note_impact_facts_block", return_value=""), \
         patch.object(assist_agent, "_note_impact_project_block", new=AsyncMock(return_value="")), \
         patch.object(assist_replan, "analyze_note_impact",
                      new=AsyncMock(return_value={"affected": affected})), \
         patch.object(assist_agent, "_stage_replan_proposal",
                      new=AsyncMock(return_value={"proposals": affected})) as stage:
        out = await assist_agent.adapt_step_to_constraint(
            session_id="S1", node_key="A1",
            constraint="NCT7904D locks PWM to automatic; manual curves impossible",
            db=_db_with_session())
    # durable constraint note recorded (kind='constraint')
    note.assert_awaited_once()
    assert note.await_args.kwargs["kind"] == "constraint"
    # the downstream step that assumed the impossible method was staged for re-plan
    stage.assert_awaited_once()
    assert [a["node_key"] for a in out["affected"]] == ["A2"]


@pytest.mark.asyncio
async def test_adapt_noop_on_empty_constraint():
    out = await assist_agent.adapt_step_to_constraint(
        session_id="S1", node_key="A1", constraint="   ", db=_db_with_session())
    assert out is None


@pytest.mark.asyncio
async def test_adapt_records_note_even_when_no_downstream_affected():
    with patch.object(settings, "assist_note_replan_enabled", True), \
         patch.object(assist_agent, "record_note", new=AsyncMock()) as note, \
         patch.object(assist_agent, "_note_impact_facts_block", return_value=""), \
         patch.object(assist_agent, "_note_impact_project_block", new=AsyncMock(return_value="")), \
         patch.object(assist_replan, "analyze_note_impact",
                      new=AsyncMock(return_value={"affected": []})), \
         patch.object(assist_agent, "_stage_replan_proposal", new=AsyncMock()) as stage:
        out = await assist_agent.adapt_step_to_constraint(
            session_id="S1", node_key="A1", constraint="a real constraint",
            db=_db_with_session())
    note.assert_awaited_once()      # constraint always recorded
    stage.assert_not_awaited()      # nothing downstream to re-plan
    assert out["affected"] == []
