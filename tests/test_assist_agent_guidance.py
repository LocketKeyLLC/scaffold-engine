"""§17.486 — tests for assist_agent's guidance wiring (generate_step_guidance,
run_step_research). The ctx assembly and the assist_guide generator are mocked;
this verifies node-key resolution, session validation, and result shaping.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_agent
from app.modules.prompt_assembly import StepContext


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
async def test_run_step_research_empty_question_raises():
    db = AsyncMock()
    with pytest.raises(ValueError, match="empty"):
        await assist_agent.run_step_research(session_id="s", question="  ", db=db)


# ── §17.487: environment ───────────────────────────────────────────────────


def test_environment_from_metadata_variants():
    assert assist_agent._environment_from_metadata(None) == {"profile": "", "substitutions": {}}
    assert assist_agent._environment_from_metadata({"other": 1}) == {"profile": "", "substitutions": {}}
    got = assist_agent._environment_from_metadata(
        {"environment": {"profile": "Ubuntu", "substitutions": {"A": "1"}}}
    )
    assert got == {"profile": "Ubuntu", "substitutions": {"A": "1"}}
    # tolerates a JSON string body
    got2 = assist_agent._environment_from_metadata('{"environment": {"profile": "X"}}')
    assert got2["profile"] == "X"


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
    assert out == {"profile": "P", "substitutions": {}, "verbosity": "normal"}


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
