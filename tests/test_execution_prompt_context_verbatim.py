"""§17.1042 — the prompt optimizer may rewrite the TASK only; upstream outputs
and grounding blocks are attached verbatim around it.

Live (parallel run 2e74196b, optimizer on by default): the "minimum tokens"
rewrite of the whole assembled prompt dropped the mandatory upstream block and
the dated sources — the report node wrote "upstream outputs were not present"
and the validator graded a report it never saw. Serial runs with
skip_optimize had the block; every Run-tab run did not.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.modules.execution_agent  # noqa: F401 — load-bearing (real app.database)

pytestmark = pytest.mark.asyncio


def _session(db):
    f = MagicMock()
    f.return_value.__aenter__ = AsyncMock(return_value=db)
    f.return_value.__aexit__ = AsyncMock(return_value=False)
    return f


async def _run(optimize_enabled: bool):
    from app.modules import execution_agent as xa
    db = AsyncMock(); db.execute = AsyncMock(); db.commit = AsyncMock()
    job = AsyncMock(return_value={"id": "job-1", "status": "running",
                                  "refined_brief": {"description": "Project P", "goals": ["G"]}})
    node = AsyncMock(return_value={
        "id": "node-2", "node_key": "T2", "title": "Write the report", "tool": "LLM",
        "prompt_template": "Write the report from T1's findings.", "domain": None,
        "depends_on": ["T1"], "assigned_model": None, "retry_count": 0,
        "last_verification_reason": None})
    captured = {}

    async def chat(messages, **kw):
        captured["prompt"] = messages[-1]["content"]
        raise RuntimeError("stop after assembly")
    opt = AsyncMock(return_value=SimpleNamespace(
        optimized_prompt="OPTIMIZED-TASK-BODY", token_count_before=50, token_count_after=5))
    set_status = AsyncMock()
    with patch.object(xa.settings, "execution_optimize_enabled", optimize_enabled), \
         patch.object(xa.settings, "best_of_n_enabled", False), \
         patch.object(xa.settings, "node_token_streaming_enabled", False), \
         patch("app.modules.execution_agent.async_session", _session(db)), \
         patch("app.modules.execution_agent._get_job", job), \
         patch("app.modules.execution_agent._get_next_node", node), \
         patch("app.modules.execution_agent._fetch_upstream_outputs",
               new=AsyncMock(return_value={"T1": ("UPSTREAM-FINDINGS-ALPHA", 0.9)})), \
         patch("app.modules.execution_agent.fetch_upstream_flagged", new=AsyncMock(return_value=set())), \
         patch("app.modules.execution_agent._fetch_rag_context", new=AsyncMock(return_value="GROUND-TRUTH-BETA")), \
         patch("app.modules.execution_agent.optimize_prompt", new=opt), \
         patch("app.modules.execution_agent.model_router.chat", new=chat), \
         patch("app.modules.execution_agent._set_node_status", new=set_status), \
         patch("app.modules.execution_agent._log_execution", new=AsyncMock()):
        await xa.execute_next_node("job-1")
    return captured["prompt"], opt, set_status


async def test_the_optimizer_sees_only_the_task_and_context_is_reattached_verbatim():
    prompt, opt, set_status = await _run(optimize_enabled=True)
    sent = opt.call_args.kwargs["prompt"]
    assert "UPSTREAM-FINDINGS-ALPHA" not in sent and "GROUND-TRUTH-BETA" not in sent
    assert "Write the report from T1's findings." in sent
    assert prompt.startswith("## Upstream Node Outputs")
    assert "UPSTREAM-FINDINGS-ALPHA" in prompt
    assert prompt.index("UPSTREAM-FINDINGS-ALPHA") < prompt.index("OPTIMIZED-TASK-BODY") < prompt.index("GROUND-TRUTH-BETA")
    # the persisted exec prompt is the same assembled text
    assert set_status.call_args.kwargs.get("optimized_prompt") == prompt


async def test_with_the_optimizer_off_the_raw_assembly_is_unchanged():
    prompt, opt, _ = await _run(optimize_enabled=False)
    assert not opt.called
    assert prompt.startswith("## Upstream Node Outputs")
    assert "Write the report from T1's findings." in prompt and prompt.rstrip().endswith("GROUND-TRUTH-BETA")
