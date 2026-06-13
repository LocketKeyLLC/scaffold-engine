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
    db = _db_with_session(sess)
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
    db = _db_with_session(sess)
    with pytest.raises(ValueError, match="no node_key"):
        await assist_agent.generate_step_guidance(session_id="s", db=db)


@pytest.mark.asyncio
async def test_generate_step_guidance_inactive_session_raises():
    sess = {"id": "s", "job_id": "j", "status": "completed", "current_node_key": "T3"}
    db = _db_with_session(sess)
    with pytest.raises(ValueError, match="cannot generate guidance"):
        await assist_agent.generate_step_guidance(session_id="s", db=db)


@pytest.mark.asyncio
async def test_run_step_research_resolves_domain():
    sess = {"id": "s", "job_id": "j", "status": "active", "current_node_key": "T3"}
    db = _db_with_session(sess, extra_rows=[{"domain": "net"}])
    with patch("app.modules.assist_guide.research_one",
               new=AsyncMock(return_value={"question": "q", "sources": [], "answer": None})) as research:
        res = await assist_agent.run_step_research(
            session_id="s", question="what flag?", db=db,
        )
    assert res["node_key"] == "T3"
    _, kwargs = research.call_args
    assert kwargs["domain"] == "net"
    assert kwargs["node_key"] == "T3"


@pytest.mark.asyncio
async def test_run_step_research_empty_question_raises():
    db = AsyncMock()
    with pytest.raises(ValueError, match="empty"):
        await assist_agent.run_step_research(session_id="s", question="  ", db=db)
