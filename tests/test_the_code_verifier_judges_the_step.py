"""§17.1790 — the code verifier judges the step's own task; the rest of the brief belongs to the other steps.

Live (job f00ea97f T19 "Implement P&L calculator"): refused for "sharpe_ratio and turnover are hardcoded None and
spy_benchmark_return is never computed … despite the brief naming SPY benchmark, Sharpe, and turnover" -- the metrics
step's (T22) work. The verifier was given the step's title and the whole brief, never the split.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.modules import execution_verify as ev


@pytest.mark.asyncio
async def test_the_prompt_carries_the_steps_task_and_the_other_steps():
    run = AsyncMock(return_value=("pass", "ok", 0.9))
    with patch.object(ev, "_run_verification", new=run):
        await ev._verify_codegen_output("Implement P&L calculator", "```python\nx = 1\n```",
                                        brief_goal="Judge against SPY: return, drawdown, Sharpe, turnover.",
                                        step_text="Python module `paper_p_l_calculator.py`: realized and unrealized P&L.",
                                        other_steps=["T22: Implement performance metrics calculator"])
    messages = run.await_args.args[0]
    user = messages[-1]["content"]
    assert "THIS STEP'S TASK:\nPython module `paper_p_l_calculator.py`" in user
    assert "OTHER STEPS OF THE PLAN" in user and "- T22: Implement performance metrics calculator" in user
    assert "judge only what THIS step's task asks for" in messages[0]["content"]


@pytest.mark.asyncio
async def test_without_a_plan_the_prompt_is_as_before():
    run = AsyncMock(return_value=("pass", "ok", 0.9))
    with patch.object(ev, "_run_verification", new=run):
        await ev._verify_codegen_output("t", "o", brief_goal="g")
    user = run.await_args.args[0][-1]["content"]
    assert "OTHER STEPS" not in user and "THIS STEP'S TASK" not in user


@pytest.mark.asyncio
async def test_the_executor_hands_the_verifier_the_steps_task():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, _run
    from app.modules import execution_agent as xa
    with patch.object(xa.settings, "codegen_verifier_strict", True), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])), \
         patch("app.sandbox.engine_tests.check", new=AsyncMock()):
        _, _, verify = await _run(GROUNDED, {"description": "Write a news CLI.", "goals": ["A CLI."]})
    kw = verify.await_args.kwargs
    assert "news_client.py" in kw["step_text"] and "other_steps" in kw
