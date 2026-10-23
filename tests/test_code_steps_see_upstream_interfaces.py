"""§17.1758 — when upstream code must shrink, a code step is given each module's interface, not its middle cut out.

Live (job 2772acae T22 "Write integration tests"): fourteen upstream modules, ~147,000 chars, each cut to head + tail.
The tests then used `result.orders`, `BacktestResult(strategy_name, model_name, …)` and
`patch("app.modules.approval_step.pending_decision")` -- names that do not exist -- and failed all three attempts.
The module below is a cut-down copy of the live T17 output's shape; names are the live ones, bodies are not.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.modules.execution_agent as xa
from app.modules.code_interface import python_interface, shrink_code

BACKTESTER = '''```python
"""Walk-forward backtester for the paper-trading lab."""
from dataclasses import dataclass, field
from app.database import async_session

DEFAULT_FEE_BPS = 1.0


@dataclass
class BacktestResult:
    """Point-in-time, cost-inclusive walk-forward backtest result."""
    strategy_name: str
    trades: list = field(default_factory=list)
    daily_equity: list = field(default_factory=list)
    order_count: int = 0

    def total_return(self) -> float:
        return 0.0


async def run_walk_forward(strategy_name: str, start_date: str, end_date: str, *, fee_bps: float = DEFAULT_FEE_BPS) -> BacktestResult:
    """Run a walk-forward backtest."""
''' + "\n".join(f"    step_{i} = {i}  # body that a cut would keep or drop at random" for i in range(400)) + '''
    return BacktestResult(strategy_name)
```'''


def test_the_interface_keeps_every_name_and_signature_and_drops_bodies():
    i = python_interface(BACKTESTER)
    assert i is not None
    for exact in ("from dataclasses import dataclass, field", "DEFAULT_FEE_BPS = 1.0", "@dataclass",
                  "class BacktestResult:", "    trades: list = field(default_factory=list)", "    order_count: int = 0",
                  "    def total_return(self) -> float: ...",
                  "async def run_walk_forward(strategy_name: str, start_date: str, end_date: str, *, "
                  "fee_bps: float=DEFAULT_FEE_BPS) -> BacktestResult: ...  # Run a walk-forward backtest."):
        assert exact in i, exact
    assert "step_399" not in i and len(i) < len(BACKTESTER) // 5


def test_text_without_python_falls_back_to_the_cut():
    assert python_interface("## A report\n\nNo code here.") is None
    assert python_interface("```python\ndef broken(:\n```") is None
    out, used = shrink_code("x" * 5000, 1000, lambda t, n: t[:n])
    assert (len(out), used) == (1000, False)


def _items():
    small = "```python\ndef approve(p):\n    return False\n```"
    return {"T14": (small, 0.9), "T17": (BACKTESTER, 0.9), "T16": (BACKTESTER.replace("BacktestResult", "AuditRow"), 0.9)}


def test_a_code_step_gets_interfaces_for_what_does_not_fit_and_whole_text_for_what_does():
    with patch.object(xa.settings, "max_upstream_chars", 4000):
        block = xa._format_upstream_block(_items(), "T22", code_interfaces=True)
        legacy = xa._format_upstream_block(_items(), "T22", code_interfaces=False)
    assert block.count("[interface only") == 2 and "[...truncated" not in block
    assert "    trades: list = field(default_factory=list)" in block            # the field the live tests guessed at
    assert "def approve(p):\n    return False" in block                         # small enough: kept whole
    assert legacy.count("[...truncated") == 2 and "[interface only" not in legacy  # every other step: unchanged


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,expected", [("CodeGen", True), ("LLM", False)])
async def test_the_executor_asks_for_interfaces_only_for_a_code_step(tool, expected):
    db = AsyncMock(); db.commit = AsyncMock()
    rows = MagicMock(); rows.mappings.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=rows)
    sess = MagicMock(); sess.return_value.__aenter__ = AsyncMock(return_value=db)
    sess.return_value.__aexit__ = AsyncMock(return_value=False)
    node = AsyncMock(return_value={
        "id": "n", "node_key": "T22", "title": "Write integration tests", "tool": tool, "prompt_template": "Test it.",
        "domain": None, "depends_on": ["T17"], "assigned_model": None, "retry_count": 0, "last_verification_reason": None})
    seen = {}
    real = xa._format_upstream_block

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    async def chat(messages, **kw):
        raise RuntimeError("stop after assembly")
    with patch.object(xa.settings, "execution_optimize_enabled", False), \
         patch.object(xa.settings, "best_of_n_enabled", False), \
         patch.object(xa.settings, "node_token_streaming_enabled", False), \
         patch("app.modules.execution_agent.async_session", sess), \
         patch("app.modules.execution_agent._get_job", AsyncMock(return_value={"id": "j", "status": "running",
                                                                              "refined_brief": {"description": "A lab."}})), \
         patch("app.modules.execution_agent._get_next_node", node), \
         patch("app.modules.execution_agent._fetch_upstream_outputs", new=AsyncMock(return_value={"T17": (BACKTESTER, 0.9)})), \
         patch("app.modules.execution_agent.fetch_upstream_flagged", new=AsyncMock(return_value=set())), \
         patch("app.modules.execution_agent._fetch_rag_context", new=AsyncMock(return_value="")), \
         patch("app.modules.execution_agent._format_upstream_block", new=spy), \
         patch("app.modules.execution_agent.model_router.chat", new=chat), \
         patch("app.modules.execution_agent._set_node_status", new=AsyncMock()), \
         patch("app.modules.execution_agent._log_execution", new=AsyncMock()):
        try:
            await xa.execute_next_node("j")
        except Exception:
            pass
    assert seen.get("code_interfaces") is expected
