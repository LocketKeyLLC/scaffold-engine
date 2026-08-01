"""§17.699 — a MAJOR divergence stages a proactive surface-and-ask re-plan.

On context_only, the background verifier detects when a submitted step's
evidence diverges from its plan; pre-§17.699 that only set an invisible
`assist_steps.divergence=TRUE`. Now it also runs the §17.677 note-impact
analyzer over the pending nodes and stages a `pending_replan`
(note_kind='divergence'), which /assist next surfaces exactly once.

Covers:
  - stage_divergence_replan: toggle-off / non-active / no-affected / analyzer
    error are all no-ops; the happy path stages a divergence-tagged proposal.
  - _take_divergence_notice: surfaces a divergence proposal once (flips
    surfaced), and leaves note/pivot proposals and already-surfaced ones alone.
"""
from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_agent, assist_replan

_JID = "91a94870-f38c-48e3-877a-225766039969"
_SID = "eba60360-4153-4c7b-a0ee-c42d99768eb1"

_AFFECTED = [
    {"node_key": "T6", "action": "drop",
     "current_assumption": "wipe all disks before install",
     "proposed_change": "no wipe — Proxmox already installed"},
]


def _result(all_=None, first_=None):
    m = MagicMock()
    m.mappings.return_value.all.return_value = all_ if all_ is not None else []
    m.mappings.return_value.first.return_value = first_
    return m


async def _stage(db, **over):
    kw = dict(
        db=db, session_id=_SID, job_id=_JID, node_key="T5",
        title="Download Proxmox ISO",
        evidence="Proxmox is already installed and reachable; skipped the download.",
        reason="the output describes system state, not a download",
    )
    kw.update(over)
    return await assist_replan.stage_divergence_replan(**kw)


# ── stage_divergence_replan ─────────────────────────────────────────────────


@pytest.mark.smoke
class TestStageDivergenceReplan:
    async def test_happy_path_stages_divergence_tagged_proposal(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(first_={"status": "active"}),  # session status
            _result(),                             # metadata UPDATE
        ])
        db.commit = AsyncMock()
        with patch.object(settings, "assist_divergence_replan_enabled", True), \
             patch.object(assist_replan, "analyze_note_impact",
                          AsyncMock(return_value={"affected": _AFFECTED})) as ana:
            out = await _stage(db)
        assert out is not None
        assert out["note_kind"] == "divergence"
        assert out["source_node"] == "T5"
        assert out["surfaced"] is False
        assert out["proposals"] == _AFFECTED
        # the operator's real result + the divergence reason both frame the note
        note_text = ana.await_args.kwargs["note_text"]
        assert "already installed" in note_text
        assert "differs from that step's original plan" in note_text
        # the metadata write embedded a divergence-tagged pending_replan
        patch_param = db.execute.await_args_list[-1].args[1]
        staged = json.loads(patch_param["patch"])["pending_replan"]
        assert staged["note_kind"] == "divergence"
        assert staged["surfaced"] is False
        db.commit.assert_awaited_once()

    async def test_toggle_off_is_noop(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        with patch.object(settings, "assist_divergence_replan_enabled", False):
            out = await _stage(db)
        assert out is None
        db.execute.assert_not_called()

    async def test_non_active_session_is_noop(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(first_={"status": "completed"})])
        with patch.object(settings, "assist_divergence_replan_enabled", True), \
             patch.object(assist_replan, "analyze_note_impact", AsyncMock()) as ana:
            out = await _stage(db)
        assert out is None
        ana.assert_not_awaited()  # never analyze a finished session

    async def test_no_affected_pending_nodes_stages_nothing(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(first_={"status": "active"})])
        db.commit = AsyncMock()
        with patch.object(settings, "assist_divergence_replan_enabled", True), \
             patch.object(assist_replan, "analyze_note_impact",
                          AsyncMock(return_value={"affected": []})):
            out = await _stage(db)
        assert out is None
        db.commit.assert_not_called()

    async def test_analyzer_error_is_fail_soft(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(first_={"status": "active"})])
        db.commit = AsyncMock()
        with patch.object(settings, "assist_divergence_replan_enabled", True), \
             patch.object(assist_replan, "analyze_note_impact",
                          AsyncMock(side_effect=RuntimeError("ollama offline"))):
            out = await _stage(db)
        assert out is None
        db.commit.assert_not_called()


# ── _take_divergence_notice ─────────────────────────────────────────────────


@pytest.mark.smoke
class TestTakeDivergenceNotice:
    async def test_surfaces_divergence_proposal_once_and_flips(self):
        meta = {"pending_replan": {
            "note_kind": "divergence", "surfaced": False,
            "source_node": "T5", "proposals": _AFFECTED,
        }}
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(first_={"metadata": meta}),  # SELECT metadata
            _result(),                            # flip UPDATE
        ])
        out = await assist_agent._take_divergence_notice(session_id=_SID, db=db)
        assert out is not None
        assert out["source_node"] == "T5"
        assert out["proposals"] == _AFFECTED
        # the flip wrote surfaced=True back so it announces exactly once
        patch_param = db.execute.await_args_list[-1].args[1]
        flipped = json.loads(patch_param["patch"])["pending_replan"]
        assert flipped["surfaced"] is True

    async def test_note_kind_proposal_is_left_untouched(self):
        # note/pivot proposals are surfaced synchronously in their own turn;
        # /assist next must NOT re-announce them.
        meta = {"pending_replan": {
            "note_kind": "constraint", "surfaced": False, "proposals": _AFFECTED,
        }}
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(first_={"metadata": meta})])
        out = await assist_agent._take_divergence_notice(session_id=_SID, db=db)
        assert out is None
        assert len(db.execute.await_args_list) == 1  # no flip UPDATE

    async def test_already_surfaced_is_noop(self):
        meta = {"pending_replan": {
            "note_kind": "divergence", "surfaced": True, "proposals": _AFFECTED,
        }}
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(first_={"metadata": meta})])
        out = await assist_agent._take_divergence_notice(session_id=_SID, db=db)
        assert out is None
        assert len(db.execute.await_args_list) == 1

    async def test_no_pending_replan_is_noop(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_result(first_={"metadata": {}})])
        out = await assist_agent._take_divergence_notice(session_id=_SID, db=db)
        assert out is None
        assert len(db.execute.await_args_list) == 1
