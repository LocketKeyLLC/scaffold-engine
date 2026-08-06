"""§17.626 — assist_agent natural-language wrappers.

classify_session_turn (grounds a message on the current step, gated by the
master toggle) and list_assist_candidates (assistable jobs for natural start).
AsyncMock DB sessions; the classifier LLM call is patched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_agent


def _result(mappings_first=None, mappings_all=None):
    r = MagicMock()
    mappings = MagicMock()
    mappings.first.return_value = mappings_first
    mappings.all.return_value = mappings_all or []
    r.mappings.return_value = mappings
    return r


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classify_turn_empty_message_is_question():
    db = AsyncMock()
    out = await assist_agent.classify_session_turn(
        session_id="s1", message="   ", db=db,
    )
    assert out["intent"] == "question"
    db.execute.assert_not_called()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classify_turn_disabled_toggle_is_question():
    db = AsyncMock()
    # `settings` is a function-local import; patch the singleton's attribute.
    with patch("app.config.settings.assist_nl_turns_enabled", False):
        out = await assist_agent.classify_session_turn(
            session_id="s1", message="I picked ZFS", db=db,
        )
    assert out["intent"] == "question"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classify_turn_no_current_step_is_question():
    db = AsyncMock()
    # session exists + active but has no current_node_key and none supplied.
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "id": "s1", "job_id": "j1", "status": "active", "current_node_key": None,
    }))
    with patch("app.config.settings.assist_nl_turns_enabled", True):
        out = await assist_agent.classify_session_turn(
            session_id="s1", message="I picked ZFS", db=db,
        )
    assert out["intent"] == "question"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classify_turn_delegates_and_threads_context():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "id": "s1", "job_id": "j1", "status": "active", "current_node_key": "T1",
    }))
    ctx = MagicMock(title="Decide storage", base_prompt="ZFS vs LVM", tool="LLM")
    with patch("app.config.settings.assist_nl_turns_enabled", True), \
         patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=({"domain": None}, ctx))), \
         patch("app.modules.assist_guide.classify_turn",
               new=AsyncMock(return_value={"intent": "submit", "evidence": "ZFS",
                                           "error_text": ""})) as classify:
        out = await assist_agent.classify_session_turn(
            session_id="s1", message="going with ZFS", db=db,
        )
    assert out["intent"] == "submit"
    assert out["node_key"] == "T1" and out["title"] == "Decide storage"
    # grounded on the current step's title/prompt/tool.
    _, kwargs = classify.call_args
    assert kwargs["title"] == "Decide storage"
    assert kwargs["task_prompt"] == "ZFS vs LVM"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_list_candidates_filters_umbrella_and_zero_node():
    rows = [
        {"id": "j1", "title": "Proxmox", "status": "assisted_running",
         "job_type": "legacy", "node_count": 9},
        {"id": "j2", "title": "Umbrella group", "status": "aggregating",
         "job_type": "umbrella", "node_count": 0},
        {"id": "j3", "title": "Empty", "status": "planning",
         "job_type": "legacy", "node_count": 0},
    ]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_all=rows))
    out = await assist_agent.list_assist_candidates(db=db)
    ids = [c["job_id"] for c in out]
    assert ids == ["j1"]  # umbrella + 0-node dropped
    assert out[0]["node_count"] == 9


@pytest.mark.asyncio
async def test_list_candidates_threads_last_activity():
    # §17.721 — the live session's last_activity_at is threaded through (ISO)
    # so the pipeline reconnect can prefer the session the operator is actually
    # mid-conversation in; jobs without a live session carry None.
    from datetime import datetime, timezone
    ts = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "j1", "title": "Proxmox", "status": "assisted_running",
         "job_type": "legacy", "node_count": 9, "last_activity_at": ts},
        {"id": "j2", "title": "Firewall", "status": "awaiting_assist",
         "job_type": "legacy", "node_count": 5, "last_activity_at": None},
    ]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_all=rows))
    out = await assist_agent.list_assist_candidates(db=db)
    assert out[0]["last_activity_at"] == ts.isoformat()
    assert out[1]["last_activity_at"] is None


# ── §17.715 — unconditional per-turn derive (gating + dedup) ────────────────


@pytest.mark.asyncio
async def test_derive_turn_memory_valve_off_noops():
    db = AsyncMock()
    with patch("app.config.settings.assist_unified_memory_enabled", False):
        out = await assist_agent.derive_turn_memory(
            session_id="s1", node_key="T2",
            message="let's do a fresh install instead", db=db)
    assert out == {"notes_added": 0, "facts_added": 0}
    db.execute.assert_not_called()          # returns before any DB work


@pytest.mark.asyncio
async def test_derive_turn_memory_trivial_turn_skips_llm_and_db():
    db = AsyncMock()
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_derive", True):
        out = await assist_agent.derive_turn_memory(
            session_id="s1", node_key="T2", message="yes", db=db)
    assert out == {"notes_added": 0, "facts_added": 0}
    db.execute.assert_not_called()          # bare control token → no extraction


@pytest.mark.asyncio
async def test_derive_turn_memory_dedups_and_records_new():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "status": "active",
        "notes": [{"kind": "decision",
                   "text": "Operator has decided to abandon in-place reconfiguration."}],
        "metadata": {"environment": {"facts": ["Existing PVE 9.2.6"]}},
    }))
    derived = {
        "notes": [
            {"kind": "decision", "text": "abandon in-place reconfiguration"},  # substring dup → skip
            {"kind": "constraint", "text": "Keep everything on a single NVMe drive."},  # new → record
        ],
        "facts": ["USB install media is prepared and plugged in"],
    }
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_derive", True), \
         patch("app.modules.assist_guide.distill_turn_memory",
               new=AsyncMock(return_value=derived)), \
         patch.object(assist_agent, "record_note",
                      new=AsyncMock(return_value={"kind": "constraint"})) as rn, \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent.derive_turn_memory(
            session_id="s1", node_key="T2",
            message="keep it all on one nvme, usb is ready", db=db)
    assert rn.await_count == 1                       # only the NEW note recorded
    assert rn.await_args.kwargs["kind"] == "constraint"
    assert out["notes_added"] == 1
    se.assert_awaited_once()                         # facts folded in once
    assert out["facts_added"] == 1


# ── §17.716 — per-message execution-context freshness ──────────────────────


@pytest.mark.asyncio
async def test_apply_shell_context_switches_auto_captured_host():
    db = AsyncMock()
    prior = assist_agent._EXEC_CTX_SENTINEL + "root@pve in ONE interactive shell …"
    with patch.object(assist_agent, "get_environment",
                      new=AsyncMock(return_value={"profile": prior})), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent._apply_shell_context(
            session_id="s1", user="root", host="DeFruscio-HomeLab", db=db)
    assert out["changed"] is True
    se.assert_awaited_once()
    assert "root@DeFruscio-HomeLab" in se.await_args.kwargs["profile"]


@pytest.mark.asyncio
async def test_apply_shell_context_respects_operator_set_profile():
    db = AsyncMock()
    with patch.object(assist_agent, "get_environment",
                      new=AsyncMock(return_value={"profile": "I use tmux with 3 panes"})), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent._apply_shell_context(
            session_id="s1", user="root", host="pve2", db=db)   # no sentinel → sacred
    assert out is None
    se.assert_not_called()


@pytest.mark.asyncio
async def test_apply_shell_context_rejects_garbage_host():
    db = AsyncMock()
    with patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent._apply_shell_context(
            session_id="s1", user="root", host="not a host!", db=db)
    assert out is None
    se.assert_not_called()


@pytest.mark.asyncio
async def test_derive_turn_memory_updates_profile_from_prose():
    # §17.716 — the reported miss: a PROSE host change (no prompt line) now
    # updates the profile via the LLM's execution_context.
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "status": "active", "notes": [],
        "metadata": {"environment": {
            "profile": assist_agent._EXEC_CTX_SENTINEL + "root@pve in ONE shell"}},
    }))
    derived = {"notes": [], "facts": [],
               "execution_context": {"user": "root", "host": "DeFruscio-HomeLab"}}
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_derive", True), \
         patch("app.modules.assist_guide.distill_turn_memory",
               new=AsyncMock(return_value=derived)), \
         patch.object(assist_agent, "_apply_shell_context", new=AsyncMock()) as ap:
        await assist_agent.derive_turn_memory(
            session_id="s1", node_key="T2",
            message="the root@DeFruscio-HomeLab now, could that be the reason", db=db)
    ap.assert_awaited_once()
    assert ap.await_args.kwargs["host"] == "DeFruscio-HomeLab"
    assert ap.await_args.kwargs["source"] == "prose"


@pytest.mark.asyncio
async def test_derive_turn_memory_captures_prompt_line_in_nonsubmit_message():
    # §17.716 — a prompt line pasted in a message (deterministic) wins over the
    # LLM prose path.
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "status": "active", "notes": [], "metadata": {"environment": {}},
    }))
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_derive", True), \
         patch("app.modules.assist_guide.distill_turn_memory",
               new=AsyncMock(return_value={"notes": [], "facts": []})), \
         patch.object(assist_agent, "_apply_shell_context", new=AsyncMock()) as ap:
        await assist_agent.derive_turn_memory(
            session_id="s1", node_key="T2",
            message="here's what i get:\nroot@DeFruscio-HomeLab:~# pvecm status\nnot ready",
            db=db)
    ap.assert_awaited_once()
    assert ap.await_args.kwargs["source"] == "turn"
    assert ap.await_args.kwargs["host"] == "DeFruscio-HomeLab"
