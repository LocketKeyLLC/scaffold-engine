"""§17.654 — tests for the one-decision-at-a-time decision prompt and the
session-level notes & additions capture.

Two concerns:
  1. A `decision` node routes to GUIDE_SYSTEM_DECISION (suggest-don't-decide,
     one choice at a time), regardless of its tool; the user prompt carries the
     decision trailer and the operator-notes block.
  2. record_note / list_notes append + read the session-level notes JSONB, and
     _coerce_notes tolerates str / None / list.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import assist_agent, assist_guide
from app.modules.prompt_assembly import StepContext


# ── decision-prompt routing ────────────────────────────────────────────────


def test_guide_system_for_tool_decision_overrides_tool():
    """A decision node ALWAYS gets the decision prompt, whatever its tool — so
    the operator is never handed a resolved-for-them runbook."""
    for tool in ("shell", "codegen", "LLM", None):
        s = assist_guide.guide_system_for_tool(tool, is_decision=True)
        assert s is assist_guide.GUIDE_SYSTEM_DECISION, tool
    # non-decision keeps the pre-§17.654 selection
    assert assist_guide.guide_system_for_tool("LLM") is assist_guide.GUIDE_SYSTEM_NONCODE


def test_decision_prompt_is_suggest_not_decide():
    """The decision prompt must frame ONE choice, offer a suggestion the operator
    can reject, and NOT resolve/bundle — the exact 'assumes too much' failure."""
    low = assist_guide.GUIDE_SYSTEM_DECISION.lower()
    assert "one decision at a time" in low
    assert "your call" in low
    assert "never auto-resolve" in low or "do not resolve" in low or "not to decide for them" in low
    # keeps the always-on beginner floor
    assert "assume no prior knowledge" in low


def _ctx(tool="LLM"):
    return StepContext(
        node_key="T2", title="Decide VLAN layout", tool=tool, domain="net",
        system_prompt="sys", base_prompt="bp", upstream_outputs={},
        upstream_truncated_keys=[], grounding="", grounding_kind=None,
        assembled_prompt="bp",
    )


def test_user_prompt_uses_decision_trailer_and_notes_block():
    notes = [{"kind": "addition", "text": "wants a DMZ segment", "node_key": "T2"}]
    user = assist_guide._build_guide_user_prompt(
        _ctx(), None, [], None, operator_notes=notes, is_decision=True,
    )
    assert assist_guide._GUIDE_DECISION_TRAILER in user
    # §17.710b — valve-agnostic: the NOTE is injected (appears in both the legacy
    # notes block and the unified session-memory block), regardless of the
    # assist_umem_inject state that this test's container env may set.
    assert "wants a DMZ segment" in user


def test_user_prompt_non_decision_keeps_walkthrough_trailer():
    user = assist_guide._build_guide_user_prompt(_ctx(), None, [], None)
    assert assist_guide._GUIDE_USER_TRAILER in user
    assert assist_guide._GUIDE_DECISION_TRAILER not in user


def test_render_operator_notes_block_empty_is_blank():
    assert assist_guide.render_operator_notes_block(None) == ""
    assert assist_guide.render_operator_notes_block([]) == ""
    # a note with no text is skipped
    assert assist_guide.render_operator_notes_block([{"text": "  "}]) == ""


# ── note capture (agent level) ─────────────────────────────────────────────


def test_coerce_notes_tolerates_str_none_list():
    assert assist_agent._coerce_notes(None) == []
    assert assist_agent._coerce_notes('[{"text":"x"}]') == [{"text": "x"}]
    assert assist_agent._coerce_notes("not json") == []
    assert assist_agent._coerce_notes([{"text": "y"}, "junk", 3]) == [{"text": "y"}]


@pytest.mark.asyncio
async def test_record_note_appends_and_commits():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=("s",))))
    db.commit = AsyncMock()
    note = await assist_agent.record_note(
        session_id="s", text_="only 2 NICs", kind="constraint", node_key="T2", db=db,
    )
    assert note == {"kind": "constraint", "node_key": "T2", "text": "only 2 NICs"}
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_note_dedupe_skips_existing():
    """§17.854 (audit C4) — dedupe=True skips the append when an identical
    (kind, text) note already exists (the re-sent-pivot case)."""
    db = AsyncMock()
    # first execute = existence check → a row (duplicate exists)
    db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=(1,))))
    db.commit = AsyncMock()
    note = await assist_agent.record_note(
        session_id="s", text_="switch to Debian", kind="decision", dedupe=True, db=db,
    )
    assert note.get("deduped") is True
    db.execute.assert_awaited_once()   # only the existence check, no append
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_note_dedupe_appends_when_new():
    """dedupe=True still appends when no identical note exists."""
    calls = []

    async def _exec(_sql, params):
        calls.append(str(_sql))
        # existence check (has jsonb_array_elements) → no row; append → a row
        exists = "jsonb_array_elements" in str(_sql)
        return MagicMock(first=MagicMock(return_value=None if exists else ("s",)))

    db = AsyncMock()
    db.execute = _exec
    db.commit = AsyncMock()
    note = await assist_agent.record_note(
        session_id="s", text_="new note", kind="decision", dedupe=True, db=db,
    )
    assert note.get("deduped") is not True and note["text"] == "new note"
    db.commit.assert_awaited_once()
    assert len(calls) == 2  # existence check + append


@pytest.mark.asyncio
async def test_record_note_empty_text_is_noop():
    db = AsyncMock()
    db.execute = AsyncMock()
    assert await assist_agent.record_note(session_id="s", text_="   ", db=db) is None
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_record_note_unknown_kind_coerced_to_note():
    captured = {}

    async def _exec(_sql, params):
        captured.update(params)
        return MagicMock(first=MagicMock(return_value=("s",)))

    db = AsyncMock()
    db.execute = _exec
    db.commit = AsyncMock()
    note = await assist_agent.record_note(
        session_id="s", text_="hi", kind="banana", db=db,
    )
    assert note["kind"] == "note"
    assert captured["kind"] == "note"


@pytest.mark.asyncio
async def test_record_note_missing_session_rolls_back():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    assert await assist_agent.record_note(session_id="nope", text_="x", db=db) is None
    db.rollback.assert_awaited_once()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_list_notes_reads_and_coerces():
    row = {"notes": [{"kind": "addition", "text": "add a DMZ"}]}
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(mappings=MagicMock(
            return_value=MagicMock(first=MagicMock(return_value=row))))
    )
    notes = await assist_agent.list_notes(session_id="s", db=db)
    assert notes == [{"kind": "addition", "text": "add a DMZ"}]


# ── classifier: note intent ────────────────────────────────────────────────


def test_note_is_a_valid_intent():
    assert "note" in assist_guide.ASSIST_INTENTS
