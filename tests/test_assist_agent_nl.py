"""§17.626 — assist_agent natural-language wrappers.

classify_session_turn (grounds a message on the current step, gated by the
master toggle) and list_assist_candidates (assistable jobs for natural start).
AsyncMock DB sessions; the classifier LLM call is patched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_agent
from app.modules import assist_environment


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
    with patch.object(assist_environment, "get_environment",
                      new=AsyncMock(return_value={"profile": prior})), \
         patch.object(assist_environment, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent._apply_shell_context(
            session_id="s1", user="root", host="DeFruscio-HomeLab", db=db)
    assert out["changed"] is True
    se.assert_awaited_once()
    assert "root@DeFruscio-HomeLab" in se.await_args.kwargs["profile"]


@pytest.mark.asyncio
async def test_apply_shell_context_respects_operator_set_profile():
    db = AsyncMock()
    with patch.object(assist_environment, "get_environment",
                      new=AsyncMock(return_value={"profile": "I use tmux with 3 panes"})), \
         patch.object(assist_environment, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent._apply_shell_context(
            session_id="s1", user="root", host="pve2", db=db)   # no sentinel → sacred
    assert out is None
    se.assert_not_called()


@pytest.mark.asyncio
async def test_apply_shell_context_rejects_garbage_host():
    db = AsyncMock()
    with patch.object(assist_environment, "set_environment", new=AsyncMock()) as se:
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


# ── §17.725 — per-turn supersession + §17.726 assistant capture/history ─────


@pytest.mark.asyncio
async def test_derive_turn_memory_applies_supersession_under_valve():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "status": "active", "notes": [],
        "metadata": {"environment": {"facts": ["P40 GPU is in IOMMU group 13"]}},
    }))
    derived = {
        "notes": [], "facts": ["Tesla P40 (02:00.0) is in IOMMU group 37"],
        "superseded": ["P40 GPU is in IOMMU group 13"],
    }
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_derive", True), \
         patch("app.config.settings.assist_umem_supersede", True), \
         patch("app.modules.assist_guide.distill_turn_memory",
               new=AsyncMock(return_value=derived)), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent.derive_turn_memory(
            session_id="s1", node_key="T2",
            message="no — the P40 is 02:00.0, in group 37", db=db)
    assert se.call_args.kwargs["retract_facts"] == ["P40 GPU is in IOMMU group 13"]
    assert out["facts_retracted"] == 1


@pytest.mark.asyncio
async def test_derive_turn_memory_supersede_valve_off_ignores_retractions():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "status": "active", "notes": [],
        "metadata": {"environment": {"facts": ["OLD"]}},
    }))
    derived = {"notes": [], "facts": ["NEW"], "superseded": ["OLD"]}
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_derive", True), \
         patch("app.config.settings.assist_umem_supersede", False), \
         patch("app.modules.assist_guide.distill_turn_memory",
               new=AsyncMock(return_value=derived)), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        await assist_agent.derive_turn_memory(
            session_id="s1", node_key="T2", message="the old fact is wrong", db=db)
    assert se.call_args.kwargs["retract_facts"] is None


@pytest.mark.asyncio
async def test_capture_assistant_reply_ingests_as_assistant_role():
    with patch.object(assist_agent, "ingest_turn",
                      new=AsyncMock(return_value=True)) as ing:
        ok = await assist_agent.capture_assistant_reply(
            session_id="s1", node_key="T2", kind="guide",
            content="walkthrough text", db=AsyncMock())
    assert ok is True
    assert ing.await_args.kwargs["role"] == "assistant"
    assert ing.await_args.kwargs["kind"] == "guide"


@pytest.mark.asyncio
async def test_capture_assistant_reply_bounds_content():
    with patch.object(assist_agent, "ingest_turn",
                      new=AsyncMock(return_value=True)) as ing:
        await assist_agent.capture_assistant_reply(
            session_id="s1", node_key=None, kind="ask",
            content="x" * 20000, db=AsyncMock())
    assert len(ing.await_args.kwargs["content"]) == 8000


@pytest.mark.asyncio
async def test_history_from_turns_maps_dedups_and_excludes_tail():
    # Rows come newest-first from the query; the helper returns oldest-first,
    # collapses the message+submit double-record, maps roles for
    # render_conversation_block, and drops the tail when it IS the current msg.
    rows = [
        {"role": "operator", "content": "what next?"},           # current msg (tail)
        {"role": "assistant", "content": "run the audit"},
        {"role": "operator", "content": "pasted output"},        # submit row
        {"role": "operator", "content": "pasted output"},        # message row (dup)
    ]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_all=rows))
    out = await assist_agent.history_from_turns(
        session_id="s1", db=db, exclude_tail="what next?")
    assert out == [
        {"role": "user", "content": "pasted output"},
        {"role": "assistant", "content": "run the audit"},
    ]


# ── §17.727 — ledger consolidation ──────────────────────────────────────────


def test_apply_fact_merges_lossless_and_positioned():
    current = [
        "oasis is a ZFS pool",              # group member (oldest)
        "Next available VM ID is 100",      # untouched
        "Storage 'oasis' active, 5.7 TB",   # group member (newest) → merged lands here
        "Tesla P40 (02:00.0) in group 37",  # untouched
    ]
    merges = [{"replaces": ["oasis is a ZFS pool", "Storage 'oasis' active, 5.7 TB"],
               "text": "Storage 'oasis' is an active ZFS pool with 5.7 TB free"}]
    out = assist_agent._apply_fact_merges(current, merges)
    assert out == [
        "Next available VM ID is 100",
        "Storage 'oasis' is an active ZFS pool with 5.7 TB free",
        "Tesla P40 (02:00.0) in group 37",
    ]


def test_apply_fact_merges_skips_degraded_groups():
    # A member vanished (retraction/cap) while the model was thinking → <2
    # present → the group is skipped and the survivor stays as-is; facts that
    # appended mid-flight are untouched.
    current = ["survivor fact", "appended while thinking"]
    merges = [{"replaces": ["survivor fact", "gone fact"], "text": "merged text"}]
    out = assist_agent._apply_fact_merges(current, merges)
    assert out == ["survivor fact", "appended while thinking"]


@pytest.mark.asyncio
async def test_consolidate_session_facts_below_threshold_skips_llm():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "metadata": {"environment": {"facts": ["a", "b", "c"]}},
    }))
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_consolidate", True), \
         patch("app.config.settings.assist_facts_consolidate_min", 30), \
         patch("app.modules.assist_guide.consolidate_facts", new=AsyncMock()) as cf:
        out = await assist_agent.consolidate_session_facts(session_id="s1", db=db)
    cf.assert_not_called()
    assert out["merges"] == 0


@pytest.mark.asyncio
async def test_consolidate_session_facts_watermark_debounces():
    # Ledger at threshold but unchanged since the last pass → no model call.
    facts = [f"F{i}" for i in range(10)]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "metadata": {"environment": {"facts": facts}, "facts_consolidated_n": 10},
    }))
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_consolidate", True), \
         patch("app.config.settings.assist_facts_consolidate_min", 5), \
         patch("app.modules.assist_guide.consolidate_facts", new=AsyncMock()) as cf:
        await assist_agent.consolidate_session_facts(session_id="s1", db=db)
    cf.assert_not_called()


@pytest.mark.asyncio
async def test_consolidate_session_facts_applies_and_watermarks():
    facts = [f"F{i}" for i in range(6)] + ["oasis pool", "oasis pool active 5.7TB"]
    meta = {"environment": {"facts": facts}}
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(mappings_first={"metadata": meta}),   # read
        _result(mappings_first={"metadata": meta}),   # FOR UPDATE re-read
        _result(mappings_first=None),                 # UPDATE
    ])
    db.commit = AsyncMock()
    merges = [{"replaces": ["oasis pool", "oasis pool active 5.7TB"],
               "text": "Storage 'oasis' is an active ZFS pool (5.7 TB)"}]
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_consolidate", True), \
         patch("app.config.settings.assist_facts_consolidate_min", 5), \
         patch("app.modules.assist_guide.consolidate_facts",
               new=AsyncMock(return_value=merges)):
        out = await assist_agent.consolidate_session_facts(session_id="s1", db=db)
    assert out["before"] == 8 and out["after"] == 7 and out["merges"] == 1
    # The written patch carries the merged ledger + the watermark.
    upd = db.execute.await_args_list[-1]
    import json as _json
    patch_arg = _json.loads(upd.args[1]["patch"])
    assert patch_arg["facts_consolidated_n"] == 7
    assert "Storage 'oasis' is an active ZFS pool (5.7 TB)" in patch_arg["environment"]["facts"]
    assert "oasis pool" not in patch_arg["environment"]["facts"]
    db.commit.assert_awaited()


def test_schedule_consolidate_facts_valve_and_threshold_gated():
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_consolidate", False):
        assist_agent.schedule_consolidate_facts(session_id="s", fact_count=100)
    assert not assist_agent._CONSOLIDATE_TASKS
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_consolidate", True), \
         patch("app.config.settings.assist_facts_consolidate_min", 30):
        assist_agent.schedule_consolidate_facts(session_id="s", fact_count=10)
    assert not assist_agent._CONSOLIDATE_TASKS


def test_fact_count_of_tolerates_mocks():
    assert assist_agent._fact_count_of({"facts": ["a", "b"]}) == 2
    assert assist_agent._fact_count_of({"facts": "notalist"}) == 0
    assert assist_agent._fact_count_of(MagicMock()) == 0
    assert assist_agent._fact_count_of(None) == 0


# ── §17.812 — capture dedupe + consolidate raced-watermark ───────────────────


@pytest.mark.asyncio
async def test_capture_assistant_reply_dedupes_backtoback_replay():
    """A cached re-present identical to the node's most recent assistant turn
    writes nothing; new content still lands."""
    from app.config import settings
    db = AsyncMock()
    scal = MagicMock()
    scal.scalar.return_value = "walkthrough text"
    db.execute = AsyncMock(return_value=scal)
    with patch.object(settings, "assist_unified_memory_enabled", True), \
         patch.object(settings, "assist_umem_capture", True), \
         patch.object(assist_agent, "ingest_turn",
                      new=AsyncMock(return_value=True)) as ing:
        ok = await assist_agent.capture_assistant_reply(
            session_id="s1", node_key="T2", kind="guide",
            content="walkthrough text", db=db)
    assert ok is False
    ing.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_assistant_reply_replay_after_other_turns_records():
    """The same walkthrough re-shown AFTER intervening dialogue IS new thread
    context — captured."""
    from app.config import settings
    db = AsyncMock()
    scal = MagicMock()
    scal.scalar.return_value = "something else was said since"
    db.execute = AsyncMock(return_value=scal)
    with patch.object(settings, "assist_unified_memory_enabled", True), \
         patch.object(settings, "assist_umem_capture", True), \
         patch.object(assist_agent, "ingest_turn",
                      new=AsyncMock(return_value=True)) as ing:
        ok = await assist_agent.capture_assistant_reply(
            session_id="s1", node_key="T2", kind="guide",
            content="walkthrough text", db=db)
    assert ok is True
    ing.assert_awaited_once()


@pytest.mark.asyncio
async def test_consolidate_raced_noop_watermarks_locked_length():
    """§17.812 — merges proposed but the ledger changed under the model so
    nothing applies: the watermark must record the LOCKED (current) length,
    not the stale pre-model snapshot, or the debounce re-fires every fold."""
    stale = {"environment": {"facts": [f"F{i}" for i in range(8)]}}
    # Under the lock the two merge members are already gone (retracted) and the
    # ledger grew elsewhere: 9 entries, none matching the merge group twice.
    fresh = {"environment": {"facts": [f"G{i}" for i in range(9)]}}
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(mappings_first={"metadata": stale}),   # read
        _result(mappings_first={"metadata": fresh}),   # FOR UPDATE re-read
        _result(mappings_first=None),                  # watermark UPDATE
    ])
    db.commit = AsyncMock()
    merges = [{"replaces": ["F1", "F2"], "text": "merged F"}]
    with patch("app.config.settings.assist_unified_memory_enabled", True), \
         patch("app.config.settings.assist_umem_consolidate", True), \
         patch("app.config.settings.assist_facts_consolidate_min", 5), \
         patch("app.modules.assist_guide.consolidate_facts",
               new=AsyncMock(return_value=merges)):
        await assist_agent.consolidate_session_facts(session_id="s1", db=db)
    import json as _json
    upd = db.execute.await_args_list[-1]
    patch_arg = _json.loads(upd.args[1]["patch"])
    assert patch_arg["facts_consolidated_n"] == 9   # locked length, not 8


# ── §17.812 (audit gap 1) — derive parity rides the capture funnel ────────────


@pytest.mark.asyncio
async def test_ingest_turn_schedules_derive_for_operator(monkeypatch):
    """A non-submit operator turn (message/fix) derives via the capture funnel
    (the derive rides ingest_turn, not only POST /turn)."""
    from app.config import settings
    monkeypatch.setattr(settings, "assist_unified_memory_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_umem_capture", True, raising=False)
    sched = MagicMock()
    with patch.object(assist_agent, "schedule_derive_turn_memory", new=sched):
        ok = await assist_agent.ingest_turn(
            session_id="s1", role="operator", kind="message",
            content="I only have 2 NICs on this box", node_key="T3", db=AsyncMock())
    assert ok is True
    sched.assert_called_once()
    assert sched.call_args.kwargs["message"] == "I only have 2 NICs on this box"
    assert sched.call_args.kwargs["node_key"] == "T3"


@pytest.mark.asyncio
async def test_ingest_turn_submit_does_not_schedule_derive(monkeypatch):
    """§17.854 (audit C4) — a 'submit' turn does NOT schedule the background
    derive; the /submit endpoint's capture_session_facts is the sole (and
    supersession-aware) fact extractor for it, avoiding two prompts per submit."""
    from app.config import settings
    monkeypatch.setattr(settings, "assist_unified_memory_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_umem_capture", True, raising=False)
    sched = MagicMock()
    with patch.object(assist_agent, "schedule_derive_turn_memory", new=sched):
        ok = await assist_agent.ingest_turn(
            session_id="s1", role="operator", kind="submit",
            content="qm create 100 done, VM boots", node_key="T3", db=AsyncMock())
    assert ok is True
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_turn_assistant_reply_not_derived(monkeypatch):
    """The engine's own words are not operator memory — no derive."""
    from app.config import settings
    monkeypatch.setattr(settings, "assist_unified_memory_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_umem_capture", True, raising=False)
    sched = MagicMock()
    with patch.object(assist_agent, "schedule_derive_turn_memory", new=sched):
        await assist_agent.ingest_turn(
            session_id="s1", role="assistant", kind="guide",
            content="here is the walkthrough", node_key="T3", db=AsyncMock())
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_derive_dedupes_recent_content(monkeypatch):
    """NL turns reach the funnel twice (message + submit/fix double-record);
    the scribe must run once per distinct content, not race itself."""
    from app.config import settings
    monkeypatch.setattr(settings, "assist_unified_memory_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_umem_derive", True, raising=False)
    assist_agent._RECENT_DERIVES.clear()
    bg = AsyncMock()
    with patch.object(assist_agent, "_derive_turn_memory_bg", new=bg):
        assist_agent.schedule_derive_turn_memory(
            session_id="s1", node_key="T1", message="the pool is created")
        assist_agent.schedule_derive_turn_memory(
            session_id="s1", node_key="T1", message="  the pool is created  ")
        assist_agent.schedule_derive_turn_memory(
            session_id="s1", node_key="T1", message="something else entirely")
        await assist_agent.drain_derive_tasks()
    assert bg.await_count == 2


@pytest.mark.asyncio
async def test_fix_endpoint_captures_operator_error():
    """§17.812 — a slash/CLI /fix records the operator's error report as a raw
    turn (it previously lived only as a truncated friction note)."""
    import types
    from app.routers import assist as assist_router
    body = types.SimpleNamespace(
        node_key="T3", error="-bash: scsi0: command not found", history=[])
    ing = AsyncMock()
    with patch.object(assist_router.assist_agent, "ingest_turn", new=ing), \
         patch.object(assist_router.assist_agent, "run_step_fix",
                      new=AsyncMock(return_value={"fix": "escape the semicolon"})):
        out = await assist_router.assist_fix("s1", body, db=AsyncMock())
    ing.assert_awaited_once()
    assert ing.call_args.kwargs["kind"] == "fix"
    assert ing.call_args.kwargs["role"] == "operator"
    assert ing.call_args.kwargs["content"] == "-bash: scsi0: command not found"
    assert out["fix"] == "escape the semicolon"


def test_assist_step_progress_real_call_no_nameerror(monkeypatch):
    """§17.812 hotfix — `_assist_step_progress` referenced `settings` without an
    import (latent since §17.811): every assist endpoint's get_session raised
    NameError once a live server actually loaded that code (unit tests all
    mocked get_session, so only the live smoke caught it). Pin the REAL call."""
    from app.config import settings
    monkeypatch.setattr(settings, "progress_eta_enabled", True, raising=False)
    out = assist_agent._assist_step_progress({"committed": 2, "pending": 2})
    assert out == {
        "phase": "assisted_executing", "label": "Assisted steps", "unit": "steps",
        "completed": 2, "total": 4, "pct": 50, "eta_ms": None, "eta_human": None,
        "current_item": None, "summary": "2/4 steps · 50%", "soft": False,
    }
    monkeypatch.setattr(settings, "progress_eta_enabled", False, raising=False)
    assert assist_agent._assist_step_progress({"committed": 2, "pending": 2}) is None
