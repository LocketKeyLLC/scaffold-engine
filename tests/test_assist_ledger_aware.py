"""§17.752 — the recap and the note-impact analyzer ground in the durable
ledgers (operator notes + observed facts), not just the node transcript / brief.

- render_facts_block: compact facts block, "" when none.
- _note_impact_facts_block: gated by assist_note_impact_facts_aware.
- summarize_step_progress: threads facts/notes into the recap prompt.
- analyze_note_impact: threads the facts block into the impact prompt.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_agent, assist_guide, assist_replan
from app.providers.base import ToolCall

_JID = "91a94870-f38c-48e3-877a-225766039969"


def _result(all_=None, first_=None):
    m = MagicMock()
    m.mappings.return_value.all.return_value = all_ if all_ is not None else []
    m.mappings.return_value.first.return_value = first_
    return m


# ── render_facts_block ──────────────────────────────────────────────────────


def test_render_facts_block_lists_facts():
    out = assist_guide.render_facts_block(
        {"facts": ["no TPM on this host", "2 NICs: eno1, eno2"]})
    assert "no TPM on this host" in out and "2 NICs" in out


def test_render_facts_block_empty_when_no_facts():
    assert assist_guide.render_facts_block({"profile": "root@pve"}) == ""
    assert assist_guide.render_facts_block(None) == ""


# ── _note_impact_facts_block gating ─────────────────────────────────────────


def test_note_impact_facts_block_gate(monkeypatch):
    md = {"environment": {"facts": ["no TPM on this host"]}}
    monkeypatch.setattr(settings, "assist_note_impact_facts_aware", True, raising=False)
    assert "no TPM" in assist_agent._note_impact_facts_block(md)
    monkeypatch.setattr(settings, "assist_note_impact_facts_aware", False, raising=False)
    assert assist_agent._note_impact_facts_block(md) == ""


# ── summarize_step_progress threads the ledgers ─────────────────────────────


@pytest.mark.asyncio
async def test_recap_prompt_includes_facts_and_notes():
    captured = {}

    async def _cun(*args, **kwargs):
        captured["messages"] = args[1] if len(args) > 1 else kwargs.get("messages")
        return types.SimpleNamespace(success=True, text="GOAL: x", model="m", error=None)

    with patch.object(assist_guide, "chat_until_nonempty", new=_cun):
        out = await assist_guide.summarize_step_progress(
            title="net", transcript="operator: did the thing",
            facts_block="Known facts about the operator's system (observed):\n- no TPM",
            notes_block="- (constraint) only 2 NICs",
        )
    assert out == "GOAL: x"
    user_msg = captured["messages"][1]["content"]
    assert "no TPM" in user_msg
    assert "only 2 NICs" in user_msg


# ── analyze_note_impact threads the facts block ─────────────────────────────


@pytest.mark.asyncio
async def test_note_impact_prompt_grounds_in_facts():
    rows = [{"node_key": "T1", "title": "Enable Secure Boot", "description": "needs TPM"}]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(all_=rows),                                   # pending nodes
        _result(first_={"refined_brief": {"goals": ["g1"]}}),  # brief
    ])
    resp = types.SimpleNamespace(
        text="", success=True, error=None,
        tool_calls=[ToolCall(id="t0", name="record_plan_impact", arguments={"affected": []})],
    )
    with patch("app.model_router.tool_call", AsyncMock(return_value=resp)) as tc:
        await assist_replan.analyze_note_impact(
            db=db, job_id=_JID, note_text="disable secure boot", note_kind="decision",
            facts_block="Known facts about the operator's system (observed):\n- no TPM on this host",
        )
    prompt = tc.await_args.kwargs["messages"][0]["content"]
    assert "no TPM on this host" in prompt
    assert "judge impact against this reality" in prompt.lower()


@pytest.mark.asyncio
async def test_note_impact_prompt_omits_facts_when_absent():
    rows = [{"node_key": "T1", "title": "Enable Secure Boot", "description": None}]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(all_=rows),
        _result(first_=None),
    ])
    resp = types.SimpleNamespace(
        text="", success=True, error=None,
        tool_calls=[ToolCall(id="t0", name="record_plan_impact", arguments={"affected": []})],
    )
    with patch("app.model_router.tool_call", AsyncMock(return_value=resp)) as tc:
        await assist_replan.analyze_note_impact(
            db=db, job_id=_JID, note_text="x", note_kind="decision",
        )
    prompt = tc.await_args.kwargs["messages"][0]["content"]
    assert "judge impact against this reality" not in prompt.lower()
