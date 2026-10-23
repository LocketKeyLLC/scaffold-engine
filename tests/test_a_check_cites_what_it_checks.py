"""§17.1755 — a validation step is asked to cite the code it checks (its upstream), not every CodeGen step in the job.

Live (job 2772acae T27 "Validate secrets handling", depends on T9/T10/T15): the §17.377 guard read every done CodeGen
node in the job, so T27 was told to cite T11–T19 (risk controls, backtester, metrics…) in its claims about secrets.
Its third attempt cited exactly T9, T10 and T15 and was still failed: "PREVIOUSLY CITED: ['T10', 'T15', 'T9']
MISSING: ['T11', 'T12', 'T13', …]".
"""
from __future__ import annotations

from app.modules.execution_verify import check_validation_citation_coverage, upstream_codegen_keys


def _n(key, deps=(), tool="CodeGen", status="done"):
    return {"node_key": key, "depends_on": list(deps), "tool": tool, "status": status}


# the live job's shape: T27 checks the Alpaca client (T9), its stream (T10) and the order executor (T15)
ROWS = [
    _n("T9"), _n("T10", ["T9"]), _n("T11"), _n("T12", ["T9", "T11"]), _n("T13", ["T11"]),
    _n("T14", ["T13"]), _n("T15", ["T9", "T14", "T11", "T12"]), _n("T17", ["T11"]), _n("T19", ["T9"]),
    _n("T1", tool="LLM"), _n("T20", ["T17", "T19"], status="pending"),
    _n("T27", ["T9", "T10", "T15"], tool="LLM", status="running"),
]

LIVE_THIRD_ATTEMPT = """
- MET (T9): broker keys are read with runner_secrets.values_for and never logged.
- MET (T10): the stream authenticates with the same keys and never prints them.
- MET (T15): the executor passes no key into any prompt or log line.
"""


def test_the_validator_is_asked_for_the_code_it_checks():
    """The nearest code upstream: T15's own imports (T11–T14) are T15's concern, not the secrets check's."""
    assert upstream_codegen_keys("T27", ROWS) == ["T9", "T10", "T15"]


def test_the_live_third_attempt_now_passes_and_failed_before():
    assert check_validation_citation_coverage(LIVE_THIRD_ATTEMPT, upstream_codegen_keys("T27", ROWS)) == []
    # the job-wide set it was held to before: every done CodeGen step
    job_wide = [r["node_key"] for r in ROWS if r["tool"] == "CodeGen" and r["status"] == "done"]
    assert "T17" in check_validation_citation_coverage(LIVE_THIRD_ATTEMPT, job_wide)


def test_a_validator_with_no_edges_keeps_the_job_wide_set():
    rows = ROWS + [_n("V1", tool="LLM", status="running")]
    assert upstream_codegen_keys("V1", rows) == ["T9", "T10", "T11", "T12", "T13", "T14", "T15", "T17", "T19"]


def test_pending_code_is_not_required_and_non_code_steps_are_walked_through():
    rows = ROWS + [_n("V2", ["T20", "T1"], tool="LLM", status="running")]
    # T20 is CodeGen but pending: it ends the walk and is not required (not done); T1 is walked through (no deps)
    assert upstream_codegen_keys("V2", rows) == []
    rows2 = ROWS + [_n("D1", ["T17", "T19"], tool="LLM"), _n("V3", ["D1"], tool="LLM", status="running")]
    assert upstream_codegen_keys("V3", rows2) == ["T17", "T19"]     # through a non-code step to the code behind it


# ── the production path ──────────────────────────────────────────────────────

async def _run_validator(output: str):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch
    import app.modules.execution_agent as xa
    db = AsyncMock(); db.commit = AsyncMock()
    rows = MagicMock(); rows.mappings.return_value.all.return_value = [dict(r) for r in ROWS]
    rows.fetchall.return_value = [(r["node_key"],) for r in ROWS if r["tool"] == "CodeGen" and r["status"] == "done"]
    db.execute = AsyncMock(return_value=rows)
    sess = MagicMock(); sess.return_value.__aenter__ = AsyncMock(return_value=db)
    sess.return_value.__aexit__ = AsyncMock(return_value=False)
    job = AsyncMock(return_value={"id": "job-1", "status": "running", "refined_brief": {"description": "A lab."}})
    node = AsyncMock(return_value={
        "id": "node-27", "node_key": "T27", "title": "Validate secrets handling", "tool": "LLM", "node_type": "checkpoint",
        "prompt_template": "Check the broker keys are handled safely in T9, T10 and T15.", "domain": None,
        "depends_on": ["T9", "T10", "T15"], "assigned_model": None, "retry_count": 0, "last_verification_reason": None})

    async def chat(messages, **kw):
        return SimpleNamespace(text=output, success=True, model="m", error=None, tool_calls=None,
                               fallback_used=False, prompt_tokens=1, completion_tokens=1, retries=0)
    set_status = AsyncMock()
    with patch.object(xa.settings, "execution_optimize_enabled", False), \
         patch.object(xa.settings, "best_of_n_enabled", False), \
         patch.object(xa.settings, "node_token_streaming_enabled", False), \
         patch("app.modules.execution_agent.async_session", sess), \
         patch("app.modules.execution_agent._get_job", job), \
         patch("app.modules.execution_agent._get_next_node", node), \
         patch("app.modules.execution_agent._fetch_upstream_outputs", new=AsyncMock(return_value={})), \
         patch("app.modules.execution_agent.fetch_upstream_flagged", new=AsyncMock(return_value=set())), \
         patch("app.modules.execution_agent._fetch_rag_context", new=AsyncMock(return_value="")), \
         patch("app.modules.execution_agent.model_router.chat", new=chat), \
         patch("app.modules.execution_agent._verify_output", new=AsyncMock(return_value=("pass", "ok", 0.9))), \
         patch("app.modules.execution_agent._set_node_status", new=set_status), \
         patch("app.modules.execution_agent._log_execution", new=AsyncMock()):
        try:
            await xa.execute_next_node("job-1")
        except Exception:
            pass
    return [str(c.kwargs.get("verification_reason") or "") for c in set_status.call_args_list]


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_the_executor_holds_the_live_validator_to_its_own_scope():
    reasons = await _run_validator(LIVE_THIRD_ATTEMPT)
    assert not [r for r in reasons if "§17.377" in r], reasons


@pytest.mark.asyncio
async def test_the_executor_still_fails_a_validator_that_skips_what_it_checks():
    reasons = await _run_validator("- MET (T9): keys come from runner_secrets.\n")
    hit = [r for r in reasons if "§17.377" in r]
    assert hit and "T10" in hit[0] and "T15" in hit[0] and "T17" not in hit[0], reasons
