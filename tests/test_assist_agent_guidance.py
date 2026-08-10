"""§17.486 — tests for assist_agent's guidance wiring (generate_step_guidance,
run_step_research). The ctx assembly and the assist_guide generator are mocked;
this verifies node-key resolution, session validation, and result shaping.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings as _settings
from app.modules import assist_agent
from app.modules.prompt_assembly import StepContext


def _result_rowcount(n):
    r = MagicMock()
    r.rowcount = n
    return r


def _ctx():
    return StepContext(
        node_key="T3", title="Install proxy", tool="shell", domain="net",
        system_prompt="sys", base_prompt="bp", upstream_outputs={},
        upstream_truncated_keys=[], grounding="", grounding_kind=None,
        assembled_prompt="bp",
    )


def _result(row):
    r = MagicMock()
    r.mappings.return_value.first.return_value = row
    return r


def _db_with_session(sess_row, extra_rows=None):
    """db.execute returns the session row first, then any extra rows in order."""
    rows = [sess_row] + list(extra_rows or [])
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(r) for r in rows])
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_generate_step_guidance_resolves_current_node():
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3"}
    # §17.639 — the anti-echo guard SELECTs the current step's status; a live
    # (non-terminal) pointer is used as-is with no healing.
    db = _db_with_session(sess, extra_rows=[{"status": "presented"}])
    node_row = {"description": "desc", "domain": "net"}
    with patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=(node_row, _ctx()))), \
         patch("app.modules.assist_guide.ensure_guidance",
               new=AsyncMock(return_value={"guidance": "walk", "status": "ready",
                                           "cached": False, "guidance_meta": {}})) as ensure:
        res = await assist_agent.generate_step_guidance(
            session_id="s", research=False, force=False, db=db,
        )
    assert res["node_key"] == "T3"          # resolved from current_node_key
    assert res["status"] == "ready"
    assert res["tool"] == "shell"
    # node_description + domain threaded through from the node row
    _, kwargs = ensure.call_args
    assert kwargs["node_description"] == "desc"
    assert kwargs["domain"] == "net"
    assert kwargs["force"] is False


@pytest.mark.asyncio
async def test_generate_step_guidance_missing_session_raises():
    db = _db_with_session(None)
    with pytest.raises(ValueError, match="not found"):
        await assist_agent.generate_step_guidance(session_id="s", db=db)


@pytest.mark.asyncio
async def test_generate_step_guidance_no_node_raises():
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": None}
    # No pointer + no pending step → the guard's _next_pending_node_key SELECT
    # returns nothing → "no live step".
    db = _db_with_session(sess, extra_rows=[None])
    with pytest.raises(ValueError, match="no live step"):
        await assist_agent.generate_step_guidance(session_id="s", db=db)


@pytest.mark.asyncio
async def test_generate_step_guidance_heals_past_terminal_pointer():
    """§17.639 — pointer lingers on a finished step (e.g. handed_off / committed)
    and no explicit node_key is given → the guard heals forward to the next
    pending step and generates for THAT, never re-rendering the finished one
    (the "output is echoing" class)."""
    for terminal in ("committed", "handed_off", "skipped"):
        sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3"}
        # execute order: session, step-status(terminal), next_pending(T5), heal UPDATE
        db = _db_with_session(
            sess, extra_rows=[{"status": terminal}, {"node_key": "T5"}, {}],
        )
        node_row = {"description": "d", "domain": "net"}
        with patch.object(assist_agent, "_assemble_ctx_for_node",
                          new=AsyncMock(return_value=(node_row, _ctx()))), \
             patch("app.modules.assist_guide.ensure_guidance",
                   new=AsyncMock(return_value={"guidance": "walk", "status": "ready",
                                               "cached": False, "guidance_meta": {}})):
            res = await assist_agent.generate_step_guidance(
                session_id="s", research=False, force=False, db=db,
            )
        assert res["node_key"] == "T5", f"{terminal}: should heal forward, not echo"
        db.commit.assert_awaited()  # corrected pointer persisted


@pytest.mark.asyncio
async def test_generate_step_guidance_explicit_node_key_honored_on_terminal():
    """§17.639 — an EXPLICIT node_key is honored even when it names a finished
    step (intentional re-view via `/assist guide T1`); no status query, no heal."""
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T5"}
    db = _db_with_session(sess)  # only the session SELECT — guard short-circuits
    node_row = {"description": "d", "domain": "net"}
    with patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=(node_row, _ctx()))), \
         patch("app.modules.assist_guide.ensure_guidance",
               new=AsyncMock(return_value={"guidance": "walk", "status": "ready",
                                           "cached": False, "guidance_meta": {}})):
        res = await assist_agent.generate_step_guidance(
            session_id="s", node_key="T1", research=False, force=False, db=db,
        )
    assert res["node_key"] == "T1"


@pytest.mark.asyncio
async def test_generate_step_guidance_inactive_session_raises():
    sess = {"id": "s", "job_id": "j", "status": "completed", "current_node_key": "T3"}
    db = _db_with_session(sess)
    with pytest.raises(ValueError, match="cannot generate guidance"):
        await assist_agent.generate_step_guidance(session_id="s", db=db)


@pytest.mark.asyncio
async def test_run_step_research_resolves_domain():
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3"}
    # execute order: session SELECT, domain SELECT, refined_brief SELECT.
    # (§17.650) — the project-digest fetch is patched out below.
    db = _db_with_session(
        sess,
        extra_rows=[{"domain": "net"}, {"refined_brief": {"description": "connect two PCs"}}],
    )
    with patch("app.modules.assist_guide.research_one",
               new=AsyncMock(return_value={"question": "q", "sources": [], "answer": None})) as research, \
         patch("app.modules.assist_agent._job_digest_for",
               new=AsyncMock(return_value="## Project context — done work")):
        res = await assist_agent.run_step_research(
            session_id="s", question="what flag?", db=db,
        )
    assert res["node_key"] == "T3"
    _, kwargs = research.call_args
    assert kwargs["domain"] == "net"
    assert kwargs["node_key"] == "T3"
    # §17.650 — project state is threaded into the research call, not dropped.
    assert "Project context" in (kwargs["job_context"] or "")
    assert "connect two PCs" in kwargs["job_context"]


@pytest.mark.asyncio
async def test_run_step_research_grounds_on_operator_notes_with_supersession():
    """§17.720 — the ask/research path was NOTES-blind: it injected only the
    legacy env block, so a session whose notes recorded the operator's pivot
    ("set up the new Proxmox ISO first") kept answering from the brief's
    in-place plan ("your existing installation…"). The path must inject the
    unified memory — notes + facts — and the §17.714 reset supersession must
    engage on the recorded pivot note."""
    sess = {
        "id": "s", "job_id": "j", "status": "active", "current_node_key": "T1",
        "notes": [{"kind": "decision",
                   "text": "Operator has decided to set up the new Proxmox ISO "
                           "first before other tasks."}],
        "metadata": {"environment": {
            "profile": "",
            "facts": ["A flash drive with the Proxmox ISO is plugged into the server."],
            "substitutions": {},
        }},
    }
    db = _db_with_session(
        sess,
        extra_rows=[{"domain": "net"},
                    {"refined_brief": {"description": "In-place cleanup of the existing Proxmox host"}}],
    )
    with patch("app.modules.assist_guide.research_one",
               new=AsyncMock(return_value={"question": "q", "sources": [], "answer": None})) as research, \
         patch("app.modules.assist_agent._job_digest_for", new=AsyncMock(return_value="")), \
         patch.object(_settings, "assist_unified_memory_enabled", True), \
         patch.object(_settings, "assist_umem_inject", True):
        await assist_agent.run_step_research(
            session_id="s", question="which installer option should I choose?", db=db,
        )
    ctx = research.call_args.kwargs["job_context"] or ""
    assert "new Proxmox ISO" in ctx                 # the operator's pivot reaches the answer
    assert "flash drive" in ctx                     # facts still present
    assert "CHANGED DIRECTION" in ctx               # §17.714 supersession engaged


@pytest.mark.asyncio
async def test_run_step_research_notes_reach_context_on_legacy_path_too():
    """§17.720 — with the unified valves OFF, the legacy branch of
    _render_memory_or_legacy still carries the operator-notes block (the old
    code passed no notes at all)."""
    sess = {
        "id": "s", "job_id": "j", "status": "active", "current_node_key": "T1",
        "notes": [{"kind": "decision", "text": "Use the new Proxmox ISO on the USB."}],
        "metadata": {"environment": {"profile": "root@pve", "facts": [], "substitutions": {}}},
    }
    db = _db_with_session(
        sess, extra_rows=[{"domain": "net"}, {"refined_brief": {"description": "goal"}}],
    )
    with patch("app.modules.assist_guide.research_one",
               new=AsyncMock(return_value={"question": "q", "sources": [], "answer": None})) as research, \
         patch("app.modules.assist_agent._job_digest_for", new=AsyncMock(return_value="")), \
         patch.object(_settings, "assist_unified_memory_enabled", False):
        await assist_agent.run_step_research(session_id="s", question="q?", db=db)
    ctx = research.call_args.kwargs["job_context"] or ""
    assert "new Proxmox ISO" in ctx
    assert "root@pve" in ctx


@pytest.mark.asyncio
async def test_run_step_research_empty_question_raises():
    db = AsyncMock()
    with pytest.raises(ValueError, match="empty"):
        await assist_agent.run_step_research(session_id="s", question="  ", db=db)


# ── §17.487: environment ───────────────────────────────────────────────────


def test_environment_from_metadata_variants():
    assert assist_agent._environment_from_metadata(None) == {
        "profile": "", "substitutions": {}, "facts": []}
    assert assist_agent._environment_from_metadata({"other": 1}) == {
        "profile": "", "substitutions": {}, "facts": []}
    got = assist_agent._environment_from_metadata(
        {"environment": {"profile": "Ubuntu", "substitutions": {"A": "1"},
                         "facts": ["Existing PVE 9.2.6"]}}
    )
    assert got == {"profile": "Ubuntu", "substitutions": {"A": "1"},
                   "facts": ["Existing PVE 9.2.6"]}
    # tolerates a JSON string body
    got2 = assist_agent._environment_from_metadata('{"environment": {"profile": "X"}}')
    assert got2["profile"] == "X"
    assert got2["facts"] == []  # §17.709 — always a list


@pytest.mark.asyncio
async def test_set_environment_appends_and_dedups_facts():
    # §17.709 — facts append to the ledger, de-dup case-insensitively.
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"metadata": {"environment": {
            "profile": "", "substitutions": {}, "facts": ["Existing PVE 9.2.6"]}}}),
        _result(None),
    ])
    db.commit = AsyncMock()
    out = await assist_agent.set_environment(
        session_id="s",
        facts=["existing pve 9.2.6", "Network: vmbr0 = 192.168.1.156/24"],  # 1st is a dup
        db=db,
    )
    assert out["facts"] == ["Existing PVE 9.2.6", "Network: vmbr0 = 192.168.1.156/24"]


@pytest.mark.asyncio
async def test_capture_session_facts_distills_and_stores():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(
        {"title": "Audit", "prompt_template": "run the audit"}))
    with patch("app.modules.assist_guide.distill_facts",
               new=AsyncMock(return_value={"facts": ["Existing Proxmox (not fresh)"], "superseded": []})), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.capture_session_facts(
            session_id="s", node_key="T1",
            evidence="root@pve:~# pveversion\npve-manager/9.2.6", db=db,
        )
    assert out == ["Existing Proxmox (not fresh)"]
    assert setenv.call_args.kwargs["facts"] == ["Existing Proxmox (not fresh)"]


@pytest.mark.asyncio
async def test_capture_session_facts_no_facts_skips_write():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result({"title": "x", "prompt_template": "y"}))
    with patch("app.modules.assist_guide.distill_facts",
               new=AsyncMock(return_value={"facts": [], "superseded": []})), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.capture_session_facts(
            session_id="s", node_key="T1", evidence="ok", db=db,
        )
    assert out == []
    setenv.assert_not_called()


@pytest.mark.asyncio
async def test_set_environment_merges_substitutions():
    # First execute = SELECT metadata FOR UPDATE (existing profile + one sub),
    # second = UPDATE.
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"metadata": {"environment": {"profile": "Ubuntu", "substitutions": {"A": "1"}}}}),
        _result(None),
    ])
    db.commit = AsyncMock()
    out = await assist_agent.set_environment(
        session_id="s", substitutions={"B": "2"}, db=db,
    )
    assert out["profile"] == "Ubuntu"            # untouched
    assert out["substitutions"] == {"A": "1", "B": "2"}  # merged, not clobbered
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_environment_missing_session_raises():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(None)])
    with pytest.raises(ValueError, match="not found"):
        await assist_agent.set_environment(session_id="s", profile="x", db=db)


@pytest.mark.asyncio
async def test_get_environment_returns_shape():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"metadata": {"environment": {"profile": "P", "substitutions": {}}}}),
    ])
    out = await assist_agent.get_environment(session_id="s", db=db)
    assert out == {"profile": "P", "substitutions": {}, "facts": [], "verbosity": "normal"}


# ── §17.487: verify_submit_outcome ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_submit_outcome_presented_returns_verdict():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"status": "presented", "metadata": {}, "title": "t",
                 "prompt_template": "p", "tool": "shell"}),
    ])
    with patch("app.modules.assist_guide.verify_step_success",
               new=AsyncMock(return_value={"outcome": "failed", "reason": "err", "suggestion": "s"})):
        v = await assist_agent.verify_submit_outcome(
            session_id="s", node_key="T2", evidence="boom", db=db,
        )
    assert v["outcome"] == "failed"


@pytest.mark.asyncio
async def test_verify_submit_outcome_not_presented_returns_none():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"status": "committed", "metadata": {}, "title": "t",
                 "prompt_template": "p", "tool": "shell"}),
    ])
    with patch("app.modules.assist_guide.verify_step_success", new=AsyncMock()) as vs:
        v = await assist_agent.verify_submit_outcome(
            session_id="s", node_key="T2", evidence="x", db=db,
        )
    assert v is None
    vs.assert_not_called()  # no LLM call when the step isn't claimable


# ── §17.487: run_step_fix ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_step_fix_resolves_node_and_records_friction():
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3",
            "metadata": {"environment": {"profile": "Ubuntu", "substitutions": {}}}}
    db = _db_with_session(sess)
    node_row = {"description": "d", "domain": "net"}
    with patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=(node_row, _ctx()))), \
         patch("app.modules.assist_guide.generate_fix",
               new=AsyncMock(return_value={"fix": "## Diagnosis\nx", "status": "ready",
                                           "guidance_meta": {}})) as gen, \
         patch.object(assist_agent, "record_friction", new=AsyncMock()) as fric:
        res = await assist_agent.run_step_fix(
            session_id="s", error="command not found", research=False, db=db,
        )
    assert res["status"] == "ready"
    assert res["node_key"] == "T3"
    _, kwargs = gen.call_args
    assert kwargs["error_text"] == "command not found"
    assert kwargs["environment"]["profile"] == "Ubuntu"
    fric.assert_awaited_once()  # blocker captured on the friction trail


@pytest.mark.asyncio
async def test_run_step_fix_empty_error_raises():
    db = AsyncMock()
    with pytest.raises(ValueError, match="empty"):
        await assist_agent.run_step_fix(session_id="s", error="  ", db=db)


# ── §17.490: learn_from_submit ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_learn_from_submit_no_cached_guidance_returns_empty():
    db = AsyncMock()
    with patch("app.modules.assist_guide.read_cached_guidance",
               new=AsyncMock(return_value=None)), \
         patch("app.modules.assist_guide.extract_substitutions", new=AsyncMock()) as ex:
        out = await assist_agent.learn_from_submit(
            session_id="s", node_key="T2", evidence="x", db=db,
        )
    assert out == {}
    ex.assert_not_called()  # nothing to learn without guidance


@pytest.mark.asyncio
async def test_learn_from_submit_only_adds_new_keys():
    db = AsyncMock()
    with patch("app.modules.assist_guide.read_cached_guidance",
               new=AsyncMock(return_value={"guidance": "ssh <HOST_IP>; <PORT>"})), \
         patch("app.modules.assist_guide.extract_substitutions",
               new=AsyncMock(return_value={"HOST_IP": "10.0.0.9", "PORT": "8080"})), \
         patch.object(assist_agent, "get_environment",
                      new=AsyncMock(return_value={"profile": "", "substitutions": {"HOST_IP": "10.0.0.5"}})), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.learn_from_submit(
            session_id="s", node_key="T2", evidence="...", db=db,
        )
    # HOST_IP already set by the operator → not overwritten; only PORT is new.
    assert out == {"PORT": "8080"}
    _, kwargs = setenv.call_args
    assert kwargs["substitutions"] == {"PORT": "8080"}


@pytest.mark.asyncio
async def test_learn_from_submit_nothing_new_skips_write():
    db = AsyncMock()
    with patch("app.modules.assist_guide.read_cached_guidance",
               new=AsyncMock(return_value={"guidance": "<HOST_IP>"})), \
         patch("app.modules.assist_guide.extract_substitutions",
               new=AsyncMock(return_value={"HOST_IP": "10.0.0.5"})), \
         patch.object(assist_agent, "get_environment",
                      new=AsyncMock(return_value={"profile": "", "substitutions": {"HOST_IP": "10.0.0.5"}})), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.learn_from_submit(
            session_id="s", node_key="T2", evidence="...", db=db,
        )
    assert out == {}
    setenv.assert_not_called()  # no new keys → no write


# ── §17.703: capture_execution_context (execution-environment monitor) ─────


def _capture_env(profile: str):
    """Patch get_environment to a session whose profile is `profile`."""
    return patch.object(
        assist_agent, "get_environment",
        new=AsyncMock(return_value={"profile": profile, "substitutions": {}}),
    )


@pytest.mark.asyncio
async def test_capture_execution_context_captures_when_empty():
    db = AsyncMock()
    with _capture_env(""), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.capture_execution_context(
            session_id="s", evidence="root@pve:~# ls -la", db=db,
        )
    assert out == {"user": "root", "host": "pve", "changed": False}
    profile = setenv.call_args.kwargs["profile"]
    assert profile.startswith(assist_agent._EXEC_CTX_SENTINEL)
    assert "root@pve" in profile


@pytest.mark.asyncio
async def test_capture_execution_context_captures_from_failed_paste():
    # The reported bug: an error paste still carries the real prompt. It MUST
    # capture (the router calls this before the failed-verdict early return).
    db = AsyncMock()
    evidence = "root@pve:/etc/pve# systemctl start x\nJob failed. See systemctl status."
    with _capture_env(""), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.capture_execution_context(
            session_id="s", evidence=evidence, db=db,
        )
    assert out and out["host"] == "pve"
    setenv.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_execution_context_no_prompt_is_noop():
    db = AsyncMock()
    with patch.object(assist_agent, "get_environment", new=AsyncMock()) as getenv, \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.capture_execution_context(
            session_id="s", evidence="I finished the step, it worked", db=db,
        )
    assert out is None
    getenv.assert_not_called()  # short-circuits before any DB read
    setenv.assert_not_called()


@pytest.mark.asyncio
async def test_capture_execution_context_same_host_noop():
    db = AsyncMock()
    prior = assist_agent._exec_context_profile("root", "pve")
    with _capture_env(prior), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.capture_execution_context(
            session_id="s", evidence="root@pve:~# whoami", db=db,
        )
    assert out is None
    setenv.assert_not_called()


@pytest.mark.asyncio
async def test_capture_execution_context_switch_updates():
    db = AsyncMock()
    prior = assist_agent._exec_context_profile("root", "pve")
    with _capture_env(prior), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.capture_execution_context(
            session_id="s", evidence="root@ct100:~# uname -a", db=db,
        )
    assert out == {"user": "root", "host": "ct100", "changed": True}
    assert "ct100" in setenv.call_args.kwargs["profile"]


@pytest.mark.asyncio
async def test_capture_execution_context_respects_operator_profile():
    db = AsyncMock()
    with _capture_env("I run ansible from a laptop across 3 nodes"), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as setenv:
        out = await assist_agent.capture_execution_context(
            session_id="s", evidence="root@pve:~# ls", db=db,
        )
    assert out is None                # explicit operator profile is sacred
    setenv.assert_not_called()


@pytest.mark.asyncio
async def test_capture_execution_context_fail_soft():
    # A raising set_environment must not propagate out of the monitor.
    db = AsyncMock()
    with _capture_env(""), \
         patch.object(assist_agent, "set_environment",
                      new=AsyncMock(side_effect=RuntimeError("db down"))):
        out = await assist_agent.capture_execution_context(
            session_id="s", evidence="root@pve:~# ls", db=db,
        )
    assert out is None


# ── §17.707: build_inputs_checklist ────────────────────────────────────────


def _result_all(rows):
    r = MagicMock()
    r.mappings.return_value.all.return_value = rows
    return r


@pytest.mark.asyncio
async def test_build_inputs_checklist():
    db = AsyncMock()
    sess = {"job_id": "j1",
            "metadata": {"environment": {"substitutions": {"HOST_IP": "10.0.0.5"}}}}
    nodes = [
        {"node_key": "T1", "node_type": "task", "title": "Audit",
         "prompt_template": "run the audit block", "status": "done"},          # not a collect step
        {"node_key": "T2", "node_type": "decision", "title": "Decide storage",
         "prompt_template": "pick zfs or lvm", "status": "pending"},           # decision, open
        {"node_key": "T3", "node_type": "task", "title": "Provide specs",
         "prompt_template": "Operator provides: model, disks", "status": "pending"},  # gather, open
        {"node_key": "T4", "node_type": "decision", "title": "Decide VLANs",
         "prompt_template": "choose ids", "status": "done"},                   # decision, done
    ]
    db.execute = AsyncMock(side_effect=[_result(sess), _result_all(nodes)])
    out = await assist_agent.build_inputs_checklist(session_id="s", db=db)
    by = {i["node_key"]: i for i in out["items"]}
    assert set(by) == {"T2", "T3", "T4"}                 # T1 (plain task) excluded
    assert by["T2"]["kind"] == "decision" and by["T2"]["done"] is False
    assert by["T3"]["kind"] == "gather" and by["T3"]["done"] is False
    assert by["T4"]["done"] is True
    assert out["open_count"] == 2 and out["total"] == 3
    assert out["provided"] == {"HOST_IP": "10.0.0.5"}


@pytest.mark.asyncio
async def test_build_inputs_checklist_missing_session():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(None)])
    with pytest.raises(ValueError, match="not found"):
        await assist_agent.build_inputs_checklist(session_id="nope", db=db)


# ── §17.710a: ingest_turn (unconditional raw capture) ──────────────────────


@pytest.mark.asyncio
async def test_ingest_turn_noop_when_valve_off():
    db = AsyncMock()
    with patch.object(_settings, "assist_unified_memory_enabled", False):
        out = await assist_agent.ingest_turn(
            session_id="s", role="operator", kind="submit", content="x", db=db)
    assert out is False
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_turn_writes_when_valve_on():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_rowcount(1))
    db.commit = AsyncMock()
    with patch.object(_settings, "assist_unified_memory_enabled", True), \
         patch.object(_settings, "assist_umem_capture", True):
        out = await assist_agent.ingest_turn(
            session_id="s", role="operator", kind="submit",
            content="root@pve:~# ls", node_key="T1", db=db)
    assert out is True
    db.commit.assert_awaited_once()
    calls = db.execute.call_args_list
    assert "INSERT INTO assist_turns" in calls[0].args[0].text
    # §17.720 — a captured turn bumps the session's activity clock so an
    # actively-chatting session doesn't rank as idle (reaper/reconnect recency).
    assert len(calls) == 2
    assert "last_activity_at = now()" in calls[1].args[0].text


@pytest.mark.asyncio
async def test_ingest_turn_skips_empty_non_skip():
    db = AsyncMock()
    with patch.object(_settings, "assist_unified_memory_enabled", True), \
         patch.object(_settings, "assist_umem_capture", True):
        out = await assist_agent.ingest_turn(
            session_id="s", role="operator", kind="submit", content="   ", db=db)
    assert out is False
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_turn_records_empty_skip():
    # A skip carries no content but IS a real turn.
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_rowcount(1))
    db.commit = AsyncMock()
    with patch.object(_settings, "assist_unified_memory_enabled", True), \
         patch.object(_settings, "assist_umem_capture", True):
        out = await assist_agent.ingest_turn(
            session_id="s", role="operator", kind="skip", content="", db=db)
    assert out is True


@pytest.mark.asyncio
async def test_ingest_turn_failsoft():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    with patch.object(_settings, "assist_unified_memory_enabled", True), \
         patch.object(_settings, "assist_umem_capture", True):
        out = await assist_agent.ingest_turn(
            session_id="s", role="operator", kind="submit", content="x", db=db)
    assert out is False


# ── §17.710c: check_submit_grounding (warn-only gate) ──────────────────────


@pytest.mark.asyncio
async def test_check_submit_grounding_noop_when_valve_off():
    db = AsyncMock()
    with patch.object(_settings, "assist_unified_memory_enabled", False):
        out = await assist_agent.check_submit_grounding(
            session_id="s", node_key="T1", evidence="x", db=db)
    assert out is None
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_check_submit_grounding_returns_reason_on_contradiction():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result({"notes": []}))
    with patch.object(_settings, "assist_unified_memory_enabled", True), \
         patch.object(_settings, "assist_umem_grounding", True), \
         patch.object(assist_agent, "get_environment",
                      new=AsyncMock(return_value={"facts": ["Existing PVE 9.2.6"],
                                                  "substitutions": {}, "profile": ""})), \
         patch("app.modules.assist_guide.check_grounding",
               new=AsyncMock(return_value={"contradicts": True, "reason": "assumes fresh"})):
        out = await assist_agent.check_submit_grounding(
            session_id="s", node_key="T1",
            evidence="Assumption: fresh Proxmox VE server", db=db)
    assert out == {"reason": "assumes fresh"}


@pytest.mark.asyncio
async def test_check_submit_grounding_none_when_consistent():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result({"notes": []}))
    with patch.object(_settings, "assist_unified_memory_enabled", True), \
         patch.object(_settings, "assist_umem_grounding", True), \
         patch.object(assist_agent, "get_environment",
                      new=AsyncMock(return_value={"facts": ["x"],
                                                  "substitutions": {}, "profile": ""})), \
         patch("app.modules.assist_guide.check_grounding",
               new=AsyncMock(return_value={"contradicts": False})):
        out = await assist_agent.check_submit_grounding(
            session_id="s", node_key="T1", evidence="looks fine", db=db)
    assert out is None


# ── §17.493: generate_step_guidance_stream ─────────────────────────────────


@pytest.mark.asyncio
async def test_generate_step_guidance_stream_resolves_and_delegates():
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3",
            "metadata": {}}
    db = _db_with_session(sess, extra_rows=[{"status": "presented"}])  # §17.639 guard

    async def _fake_stream(**kwargs):
        yield {"type": "delta", "text": "hi"}
        yield {"type": "done", "status": "ready", "guidance_meta": {}, "cached": False}

    with patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=({"description": "d", "domain": "net"}, _ctx()))), \
         patch("app.modules.assist_guide.generate_guidance_stream", new=_fake_stream):
        events = [ev async for ev in assist_agent.generate_step_guidance_stream(
            session_id="s", research=False, force=False, db=db)]
    assert events[0]["text"] == "hi"
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_generate_step_guidance_stream_missing_session_raises():
    db = _db_with_session(None)
    with pytest.raises(ValueError, match="not found"):
        # the raise fires when iteration starts
        [ev async for ev in assist_agent.generate_step_guidance_stream(session_id="s", db=db)]


# ── §17.499 — verbosity ─────────────────────────────────────────────────────


def test_verbosity_from_metadata():
    assert assist_agent._verbosity_from_metadata(None) == "normal"
    assert assist_agent._verbosity_from_metadata({"verbosity": "terse"}) == "terse"
    assert assist_agent._verbosity_from_metadata({"verbosity": "bogus"}) == "normal"
    assert assist_agent._verbosity_from_metadata('{"verbosity": "detailed"}') == "detailed"


@pytest.mark.asyncio
async def test_set_environment_sets_verbosity():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"metadata": {}}),  # FOR UPDATE read
        _result(None),              # UPDATE
    ])
    db.commit = AsyncMock()
    out = await assist_agent.set_environment(session_id="s", verbosity="detailed", db=db)
    assert out["verbosity"] == "detailed"
    # the merge patch carries verbosity
    patch_arg = db.execute.await_args_list[1].args[1]["patch"]
    assert '"verbosity": "detailed"' in patch_arg


@pytest.mark.asyncio
async def test_set_environment_rejects_bad_verbosity():
    db = AsyncMock()
    with pytest.raises(ValueError, match="verbosity must be"):
        await assist_agent.set_environment(session_id="s", verbosity="loud", db=db)


@pytest.mark.asyncio
async def test_generate_step_guidance_threads_verbosity():
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3",
            "metadata": {"verbosity": "terse"}}
    db = _db_with_session(sess, extra_rows=[{"status": "presented"}])  # §17.639 guard
    with patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=({"description": "d", "domain": None}, _ctx()))), \
         patch("app.modules.assist_guide.ensure_guidance",
               new=AsyncMock(return_value={"guidance": "w", "status": "ready",
                                           "cached": False, "guidance_meta": {}})) as ensure:
        await assist_agent.generate_step_guidance(session_id="s", research=False, db=db)
    assert ensure.call_args.kwargs["verbosity"] == "terse"


# ── §17.725 — fact supersession ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_environment_retracts_contradicted_facts():
    # §17.725 — retract_facts removes normalized matches BEFORE new facts fold
    # in; non-matching retractions are ignored.
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"metadata": {"environment": {
            "profile": "", "substitutions": {},
            "facts": ["P40 GPU is in IOMMU group 13", "ZFS pool 'oasis' active"]}}}),
        _result(None),
    ])
    db.commit = AsyncMock()
    out = await assist_agent.set_environment(
        session_id="s",
        facts=["Tesla P40 (02:00.0) is in IOMMU group 37"],
        retract_facts=["p40 gpu is in iommu group 13",   # normalized match → gone
                       "never was in the ledger"],        # ignored
        db=db,
    )
    assert out["facts"] == [
        "ZFS pool 'oasis' active",
        "Tesla P40 (02:00.0) is in IOMMU group 37",
    ]


@pytest.mark.asyncio
async def test_capture_session_facts_applies_retraction_under_valve():
    # §17.725 — with master+supersede on, the distiller sees the known ledger
    # and its verbatim retractions reach set_environment(retract_facts=…).
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(
        {"title": "Audit", "prompt_template": "run the audit"}))
    distilled = {"facts": ["Tesla P40 (02:00.0) is in IOMMU group 37"],
                 "superseded": ["P40 GPU is in IOMMU group 13"]}
    with patch.object(_settings, "assist_unified_memory_enabled", True), \
         patch.object(_settings, "assist_umem_supersede", True), \
         patch.object(assist_agent, "get_environment",
                      new=AsyncMock(return_value={
                          "facts": ["P40 GPU is in IOMMU group 13"],
                          "substitutions": {}, "profile": ""})), \
         patch("app.modules.assist_guide.distill_facts",
               new=AsyncMock(return_value=distilled)) as df, \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent.capture_session_facts(
            session_id="s", node_key="T1",
            evidence="lspci: 02:00.0 Tesla P40", db=db,
        )
    assert df.call_args.kwargs["known_facts"] == ["P40 GPU is in IOMMU group 13"]
    assert se.call_args.kwargs["retract_facts"] == ["P40 GPU is in IOMMU group 13"]
    assert out == ["Tesla P40 (02:00.0) is in IOMMU group 37"]


@pytest.mark.asyncio
async def test_capture_session_facts_valve_off_no_known_no_retract():
    # Valves off → distiller gets no known ledger, no retraction is applied.
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(
        {"title": "Audit", "prompt_template": "run the audit"}))
    with patch.object(_settings, "assist_unified_memory_enabled", False), \
         patch("app.modules.assist_guide.distill_facts",
               new=AsyncMock(return_value={"facts": ["F1"],
                                           "superseded": ["should be ignored"]})) as df, \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        await assist_agent.capture_session_facts(
            session_id="s", node_key="T1", evidence="output", db=db,
        )
    assert df.call_args.kwargs["known_facts"] is None
    assert se.call_args.kwargs["retract_facts"] is None


# ── §17.726 — assistant reply capture + transcript-fallback history ─────────


@pytest.mark.asyncio
async def test_generate_step_guidance_captures_fresh_reply():
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3",
            "metadata": {}}
    db = _db_with_session(sess, extra_rows=[{"status": "presented"}])
    with patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=({"description": "d", "domain": None}, _ctx()))), \
         patch("app.modules.assist_guide.ensure_guidance",
               new=AsyncMock(return_value={"guidance": "do the thing", "status": "ready",
                                           "cached": False, "guidance_meta": {}})), \
         patch.object(assist_agent, "capture_assistant_reply", new=AsyncMock()) as cap:
        await assist_agent.generate_step_guidance(session_id="s", research=False, db=db)
    cap.assert_awaited_once()
    assert cap.await_args.kwargs["kind"] == "guide"
    assert cap.await_args.kwargs["content"] == "do the thing"


@pytest.mark.asyncio
async def test_generate_step_guidance_cache_hit_not_recaptured():
    # A cache hit re-shows text already in the transcript — no duplicate turn.
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3",
            "metadata": {}}
    db = _db_with_session(sess, extra_rows=[{"status": "presented"}])
    with patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=({"description": "d", "domain": None}, _ctx()))), \
         patch("app.modules.assist_guide.ensure_guidance",
               new=AsyncMock(return_value={"guidance": "same text", "status": "ready",
                                           "cached": True, "guidance_meta": {}})), \
         patch.object(assist_agent, "capture_assistant_reply", new=AsyncMock()) as cap:
        await assist_agent.generate_step_guidance(session_id="s", research=False, db=db)
    cap.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_step_guidance_history_falls_back_to_transcript():
    # §17.726 — no client history + master valve on → the durable transcript
    # rebuilds the conversation block.
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3",
            "metadata": {}}
    db = _db_with_session(sess, extra_rows=[{"status": "presented"}])
    rebuilt = [{"role": "assistant", "content": "earlier walkthrough"},
               {"role": "user", "content": "it worked"}]
    with patch.object(_settings, "assist_unified_memory_enabled", True), \
         patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=({"description": "d", "domain": None}, _ctx()))), \
         patch.object(assist_agent, "history_from_turns",
                      new=AsyncMock(return_value=rebuilt)) as hft, \
         patch.object(assist_agent, "_conversation_block_for",
                      return_value="") as conv, \
         patch("app.modules.assist_guide.ensure_guidance",
               new=AsyncMock(return_value={"guidance": "g", "status": "ready",
                                           "cached": True, "guidance_meta": {}})):
        await assist_agent.generate_step_guidance(
            session_id="s", research=False, history=None, db=db)
    hft.assert_awaited_once()
    assert conv.call_args.args[0] == rebuilt


# ── §17.736 — add a guided step mid-assist ──────────────────────────────────


@pytest.mark.asyncio
async def test_add_step_inserts_node_and_points_session():
    # session, anchor (T14) lookup, brief lookup, existing-keys, INSERT node,
    # INSERT step, UPDATE anchor deps, UPDATE anchor step, UPDATE session.
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"job_id": "j1", "status": "active", "current_node_key": "T14",
                 "metadata": {}, "notes": []}),               # session
        _result({"node_key": "T14", "depends_on": ["T13"],
                 "execution_order": 14, "tool": "shell", "domain": None}),  # anchor
        _result({"refined_brief": {"description": "Build a homelab"}}),      # brief
        _result_all([{"node_key": "T13"}, {"node_key": "T14"}]),            # existing keys
        _result(None),  # INSERT dag_nodes
        _result(None),  # INSERT assist_steps
        _result(None),  # UPDATE anchor depends_on
        _result(None),  # UPDATE anchor step presented->pending
        _result(None),  # UPDATE session current_node_key
    ])
    db.commit = AsyncMock()
    with patch("app.modules.assist_guide.draft_step",
               new=AsyncMock(return_value={
                   "title": "Configure the VM's network for internet access",
                   "description": "Give VM 100 internet; done when it can ping an external host."})):
        out = await assist_agent.add_step(
            session_id="s1", request="set up the VM networking properly", db=db)
    assert out["node_key"] == "ADD1"                       # first ADD key
    assert out["before_node_key"] == "T14"
    assert "network" in out["title"].lower()
    # the anchor was made to depend on the new node, and reset to pending
    sqls = [str(c.args[0]) for c in db.execute.await_args_list]
    assert any("array_append" in s for s in sqls)          # T14 now depends on ADD1
    assert any("current_node_key = :nk" in s for s in sqls)  # session repointed
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_add_step_rejects_bad_session():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(None))
    with pytest.raises(ValueError, match="not found"):
        await assist_agent.add_step(session_id="nope", request="x", db=db)


# ── §17.738 — per-step progress recap (agent-side caching + gating) ──────────


@pytest.mark.asyncio
async def test_get_step_recap_valve_off_returns_empty():
    db = AsyncMock()
    # §17.741 — the recap gate is `step_recap OR status_panel`; pin BOTH off so
    # this stays deterministic when the container env sets ASSIST_STATUS_PANEL_
    # ENABLED=true (the §17.710d container-valve gotcha).
    with patch.object(_settings, "assist_step_recap_enabled", False), \
         patch.object(_settings, "assist_status_panel_enabled", False):
        out = await assist_agent.get_step_recap(
            session_id="s", node_key="ADD1", title="net", db=db)
    assert out == ""
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_get_step_recap_uses_cache_when_not_grown():
    # cached recap + turn count hasn't grown past the watermark → return cache,
    # no LLM call.
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"progress_recap": "GOAL: cached", "progress_recap_turns": 10}),  # step row
        _result_all([{"role": "operator", "content": "x"}] * 11),                 # 11 turns
    ])
    with patch.object(_settings, "assist_step_recap_enabled", True), \
         patch.object(_settings, "assist_step_recap_every", 3), \
         patch.object(_settings, "assist_step_recap_min_turns", 4), \
         patch("app.modules.assist_guide.summarize_step_progress",
               new=AsyncMock()) as summ:
        out = await assist_agent.get_step_recap(
            session_id="s", node_key="ADD1", title="net", db=db)
    assert out == "GOAL: cached"
    summ.assert_not_called()   # 11 < watermark(10)+every(3)=13


@pytest.mark.asyncio
async def test_get_step_recap_refreshes_when_grown():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"progress_recap": "GOAL: stale", "progress_recap_turns": 4}),   # step row
        _result_all([{"role": "operator", "content": f"m{i}"} for i in range(20)]),  # 20 turns
        # §17.752 — the ledger fetch (notes + metadata/facts) for a ledger-aware recap
        _result({"notes": [{"kind": "constraint", "text": "only 2 NICs"}],
                 "metadata": {"environment": {"facts": ["no TPM on this host"]}}}),
        _result(None),   # UPDATE recap
    ])
    db.commit = AsyncMock()
    with patch.object(_settings, "assist_step_recap_enabled", True), \
         patch.object(_settings, "assist_step_recap_every", 3), \
         patch.object(_settings, "assist_step_recap_min_turns", 4), \
         patch.object(_settings, "assist_recap_ledger_aware", True), \
         patch("app.modules.assist_guide.summarize_step_progress",
               new=AsyncMock(return_value="GOAL: fresh recap")) as summ:
        out = await assist_agent.get_step_recap(
            session_id="s", node_key="ADD1", title="net", db=db)
    assert out == "GOAL: fresh recap"
    summ.assert_awaited_once()
    # §17.752 — the durable ledgers (facts + operator constraints) reached the recap
    kw = summ.await_args.kwargs
    assert "no TPM on this host" in kw["facts_block"]
    assert "only 2 NICs" in kw["notes_block"]
    # the refreshed recap + new watermark were written
    upd = [c for c in db.execute.await_args_list if "progress_recap = :r" in str(c.args[0])]
    assert upd and upd[0].args[1]["n"] == 20


@pytest.mark.asyncio
async def test_get_step_recap_below_min_turns_no_refresh():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"progress_recap": None, "progress_recap_turns": 0}),
        _result_all([{"role": "operator", "content": "x"}] * 2),   # only 2 turns
    ])
    with patch.object(_settings, "assist_step_recap_enabled", True), \
         patch.object(_settings, "assist_step_recap_min_turns", 4), \
         patch("app.modules.assist_guide.summarize_step_progress",
               new=AsyncMock()) as summ:
        out = await assist_agent.get_step_recap(
            session_id="s", node_key="ADD1", title="net", db=db)
    assert out == ""            # nothing cached, too early to summarize
    summ.assert_not_called()


def test_render_node_transcript_dedups_and_labels():
    turns = [
        {"role": "operator", "content": "apt fails"},
        {"role": "assistant", "content": "set up NAT"},
        {"role": "operator", "content": "pasted"},   # message
        {"role": "operator", "content": "pasted"},   # submit dup → collapsed
    ]
    out = assist_agent._render_node_transcript(turns)
    assert out.count("Operator: pasted") == 1
    assert "Assistant: set up NAT" in out


def test_with_step_recap_orders_recap_first():
    out = assist_agent._with_step_recap("Recent conversation: hi", "GOAL: x")
    assert out.index("Where we are on this step") < out.index("Recent conversation")
    # either part empty is fine
    assert assist_agent._with_step_recap("", "GOAL: x").strip()
    assert assist_agent._with_step_recap("convo", "").strip() == "convo"
