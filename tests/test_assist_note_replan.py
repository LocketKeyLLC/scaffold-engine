"""§17.677 — plan-affecting notes trigger a surface-and-ask re-plan.

Covers the new machinery end-to-end at the module level:
  - analyze_note_impact: tool_call → filtered proposal; fail-soft to {affected:[]}
  - RECORD_PLAN_IMPACT_TOOL contract
  - apply_note_replan: drop → skipped; revise → description appended + guidance busted
  - assess_note_impact gate: generic note / toggle-off / no-affected → None
  - apply_pending_replan: apply mutates + clears; discard clears; no-pending is a no-op
"""
from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_agent, assist_replan
from app.providers.base import ToolCall


def _result(all_=None, first_=None):
    """A fake SQLAlchemy result exposing .mappings().all()/.first()."""
    m = MagicMock()
    m.mappings.return_value.all.return_value = all_ if all_ is not None else []
    m.mappings.return_value.first.return_value = first_
    return m


def _resp(arguments: dict):
    resp = types.SimpleNamespace()
    resp.text = ""
    resp.tool_calls = [ToolCall(id="t0", name="record_plan_impact", arguments=arguments)]
    resp.success = True
    resp.error = None
    return resp


_JID = "91a94870-f38c-48e3-877a-225766039969"
_SID = "eba60360-4153-4c7b-a0ee-c42d99768eb1"


# ── analyze_note_impact ─────────────────────────────────────────────────────


@pytest.mark.smoke
class TestAnalyzeNoteImpact:
    async def test_happy_path_filters_to_pending_keys(self):
        rows = [
            {"node_key": "T1", "title": "Install Proxmox", "description": "TPM-backed LUKS"},
            {"node_key": "T2", "title": "ZFS pool", "description": None},
        ]
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(all_=rows),
            _result(first_={"refined_brief": {"goals": ["g1"], "constraints": ["c1"]}}),
        ])
        # Model flags T1 (revise) and a hallucinated T9 (dropped by the filter).
        resp = _resp({"affected": [
            {"node_key": "T1", "action": "revise",
             "current_assumption": "TPM auto-unlock", "proposed_change": "use passphrase"},
            {"node_key": "T9", "action": "drop", "current_assumption": "x", "proposed_change": "y"},
        ]})
        with patch("app.model_router.tool_call", AsyncMock(return_value=resp)) as tc:
            out = await assist_replan.analyze_note_impact(
                db=db, job_id=_JID, note_text="no TPM available", note_kind="constraint",
            )
        assert [p["node_key"] for p in out["affected"]] == ["T1"]
        assert out["affected"][0]["action"] == "revise"
        assert out["affected"][0]["proposed_change"] == "use passphrase"
        # §17.677 — routed via model_general (reasoning task; the verifier
        # false-negatives live), with the plan-impact tool.
        kwargs = tc.await_args.kwargs
        assert kwargs["role"] == "model_general"
        assert assist_replan.RECORD_PLAN_IMPACT_TOOL in kwargs["tools"]

    async def test_no_pending_nodes_short_circuits(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(all_=[])])
        with patch("app.model_router.tool_call", AsyncMock()) as tc:
            out = await assist_replan.analyze_note_impact(
                db=db, job_id=_JID, note_text="x", note_kind="constraint",
            )
        assert out == {"affected": []}
        tc.assert_not_awaited()  # no model call when nothing is pending

    async def test_model_failure_is_fail_soft(self):
        rows = [{"node_key": "T1", "title": "t", "description": None}]
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(all_=rows), _result(first_=None)])
        with patch("app.model_router.tool_call",
                   AsyncMock(side_effect=RuntimeError("ollama offline"))):
            out = await assist_replan.analyze_note_impact(
                db=db, job_id=_JID, note_text="x", note_kind="constraint",
            )
        assert out == {"affected": []}

    async def test_empty_note_is_noop(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        out = await assist_replan.analyze_note_impact(
            db=db, job_id=_JID, note_text="   ", note_kind="constraint",
        )
        assert out == {"affected": []}
        db.execute.assert_not_called()

    async def test_bad_action_is_filtered(self):
        rows = [{"node_key": "T1", "title": "t", "description": None}]
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(all_=rows), _result(first_=None)])
        resp = _resp({"affected": [{"node_key": "T1", "action": "delete_everything"}]})
        with patch("app.model_router.tool_call", AsyncMock(return_value=resp)):
            out = await assist_replan.analyze_note_impact(
                db=db, job_id=_JID, note_text="x", note_kind="constraint",
            )
        assert out == {"affected": []}


@pytest.mark.smoke
class TestRecordPlanImpactTool:
    def test_required_and_enum(self):
        schema = assist_replan.RECORD_PLAN_IMPACT_TOOL.input_schema
        assert schema["required"] == ["affected"]
        item = schema["properties"]["affected"]["items"]
        assert set(item["properties"]["action"]["enum"]) == {"revise", "drop"}
        assert item["required"] == ["node_key", "action"]

    def test_tool_name(self):
        assert assist_replan.RECORD_PLAN_IMPACT_TOOL.name == "record_plan_impact"


# ── apply_note_replan ───────────────────────────────────────────────────────


@pytest.mark.smoke
class TestApplyNoteReplan:
    async def test_drop_marks_node_and_step_skipped(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(all_=[{"node_key": "T5"}]),  # drop dag_nodes RETURNING
            _result(),                            # drop assist_steps
        ])
        db.commit = AsyncMock()
        out = await assist_replan.apply_note_replan(
            db=db, session_id=_SID, job_id=_JID,
            proposals=[{"node_key": "T5", "action": "drop"}],
        )
        assert out == {"revised": [], "dropped": ["T5"]}
        db.commit.assert_awaited_once()

    async def test_revise_appends_description_and_busts_guidance(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(first_={"node_key": "T1"}),  # revise dag_nodes RETURNING
            _result(),                            # bust guidance
        ])
        db.commit = AsyncMock()
        out = await assist_replan.apply_note_replan(
            db=db, session_id=_SID, job_id=_JID,
            proposals=[{"node_key": "T1", "action": "revise",
                        "proposed_change": "switch to passphrase LUKS"}],
        )
        assert out == {"revised": ["T1"], "dropped": []}
        # the description UPDATE carried the concrete change
        sqls = [str(c.args[0]) for c in db.execute.await_args_list]
        params = [c.args[1] for c in db.execute.await_args_list]
        assert any("description = COALESCE(description" in s for s in sqls)
        assert any(p.get("change") == "switch to passphrase LUKS" for p in params if isinstance(p, dict))
        # a guidance-busting UPDATE ran too
        assert any("guidance_status = 'none'" in s for s in sqls)

    async def test_revise_that_touches_no_pending_row_is_dropped_from_result(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(first_=None)])  # node not pending
        db.commit = AsyncMock()
        out = await assist_replan.apply_note_replan(
            db=db, session_id=_SID, job_id=_JID,
            proposals=[{"node_key": "T1", "action": "revise", "proposed_change": "x"}],
        )
        assert out == {"revised": [], "dropped": []}


# ── assess_note_impact (agent gate) ─────────────────────────────────────────


@pytest.mark.smoke
class TestAssessNoteImpact:
    async def test_generic_note_skips_analysis(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        out = await assist_agent.assess_note_impact(
            session_id=_SID, note_kind="note", note_text="remember X", db=db,
        )
        assert out is None
        db.execute.assert_not_called()

    async def test_toggle_off_skips_analysis(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        with patch.object(settings, "assist_note_replan_enabled", False):
            out = await assist_agent.assess_note_impact(
                session_id=_SID, note_kind="constraint", note_text="no TPM", db=db,
            )
        assert out is None
        db.execute.assert_not_called()

    async def test_affected_stores_proposal_and_returns_it(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(first_={"job_id": _JID, "status": "active"}),  # session lookup
            _result(),                                              # metadata UPDATE
        ])
        db.commit = AsyncMock()
        affected = [{"node_key": "T1", "action": "revise",
                     "current_assumption": "a", "proposed_change": "b"}]
        with patch.object(settings, "assist_note_replan_enabled", True), \
             patch.object(assist_replan, "analyze_note_impact",
                          AsyncMock(return_value={"affected": affected})):
            out = await assist_agent.assess_note_impact(
                session_id=_SID, note_kind="constraint", note_text="no TPM", db=db,
            )
        assert out is not None
        assert out["proposals"] == affected
        assert out["note_kind"] == "constraint"
        # the metadata write embedded pending_replan
        patch_param = db.execute.await_args_list[-1].args[1]
        assert "pending_replan" in json.loads(patch_param["patch"])

    async def test_no_affected_returns_none_no_write(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(first_={"job_id": _JID, "status": "active"})])
        db.commit = AsyncMock()
        with patch.object(settings, "assist_note_replan_enabled", True), \
             patch.object(assist_replan, "analyze_note_impact",
                          AsyncMock(return_value={"affected": []})):
            out = await assist_agent.assess_note_impact(
                session_id=_SID, note_kind="constraint", note_text="no TPM", db=db,
            )
        assert out is None
        db.commit.assert_not_called()


# ── apply_pending_replan (agent resolve) ────────────────────────────────────


@pytest.mark.smoke
class TestApplyPendingReplan:
    async def test_apply_calls_replan_and_clears(self):
        meta = {"pending_replan": {"proposals": [{"node_key": "T1", "action": "revise"}]}}
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(first_={"job_id": _JID, "metadata": meta}),  # session read
            _result(),                                            # clear metadata key
        ])
        db.commit = AsyncMock()
        with patch.object(assist_replan, "apply_note_replan",
                          AsyncMock(return_value={"revised": ["T1"], "dropped": []})) as ap:
            out = await assist_agent.apply_pending_replan(
                session_id=_SID, decision="apply", db=db,
            )
        assert out == {"applied": True, "revised": ["T1"], "dropped": []}
        ap.assert_awaited_once()
        # the second statement removed the pending_replan key
        assert "- 'pending_replan'" in str(db.execute.await_args_list[-1].args[0])

    async def test_discard_clears_without_replan(self):
        meta = {"pending_replan": {"proposals": [{"node_key": "T1", "action": "revise"}]}}
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(first_={"job_id": _JID, "metadata": meta}),
            _result(),
        ])
        db.commit = AsyncMock()
        with patch.object(assist_replan, "apply_note_replan", AsyncMock()) as ap:
            out = await assist_agent.apply_pending_replan(
                session_id=_SID, decision="discard", db=db,
            )
        assert out == {"applied": False, "discarded": True}
        ap.assert_not_awaited()

    async def test_no_pending_is_noop(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(first_={"job_id": _JID, "metadata": {}})])
        db.commit = AsyncMock()
        out = await assist_agent.apply_pending_replan(
            session_id=_SID, decision="apply", db=db,
        )
        assert out == {"applied": False, "reason": "no_pending"}
        db.commit.assert_not_called()
