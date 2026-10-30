"""§17.1874 — an output that is tests and nothing else is redrawn for every finding; a call the test expects to raise
needs no parent row; the frontier's claim survives one slow connect.

Live (follow-up 5b4880c0 T5, 2026-10-30 17:16–17:30 UTC, the §17.1873 round): a1 was refused with 68 test-file
findings and one `SELECT … ticker … FROM fill` schema finding on the test's own SQL -- no path, so no redraw, the
attempt spent. a2 and a3 were redrawn (29->7, 90->10) and every finding left was `execute_order(approval)` under
`with pytest.raises(ValueError):` -- "`approval` is read for `approval_id`". a4 died with the frontier's claim: the
asyncpg connect handshake (2 s cap) timed out once and `execute_all_nodes` marked the JOB failed."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.modules import engine_source as es
from app.modules.engine_contracts import redraw_faked_tests

OUT = ("```python\n# tests/test_paper_twelve_modules.py\nfrom sqlalchemy import text\n\n"
       "async def test_a(db):\n    await db.execute(text('SELECT ticker FROM fill'))\n```")


async def test_a_tests_only_output_is_redrawn_even_for_a_finding_without_a_path():
    findings = ["`SELECT … ticker … FROM fill`: `fill` has no column `ticker` (it has id, price)",
                "`tests/test_paper_twelve_modules.py:6` hands `f(…)` `x` = a str, and the module reads `.json` from it"]
    asked: list[tuple[str, str]] = []

    async def ask(path, refusal, current):
        asked.append((path, refusal))
        return "# tests/test_paper_twelve_modules.py\nasync def test_a(db):\n    pass\n"

    async def check(candidate):
        return []
    new, left = await redraw_faked_tests(OUT, findings, ask=ask, check=check, tests_only=True)
    assert left == [] and asked and asked[0][0] == "tests/test_paper_twelve_modules.py"
    assert "FROM fill" in asked[0][1] and "hands `f(…)`" in asked[0][1]     # both findings shown
    assert "async def test_a(db):\n    pass" in new
    # without the flag (a module block in the output) the pathless finding still stops the redraw
    asked.clear()
    mixed = "```python\n# app/modules/paper_x.py\nX = 1\n```\n" + OUT
    _new, left = await redraw_faked_tests(mixed, findings, ask=ask, check=check)
    assert left == findings and not asked


EXECUTOR = '''# app/modules/paper_order_executor.py
from sqlalchemy import text
from app.database import async_session

async def execute_order(approval: dict) -> dict:
    proposal = approval.get("proposal", {})
    if not proposal.get("symbol"):
        raise ValueError("missing symbol")
    async with async_session() as db:
        await db.execute(text("INSERT INTO broker_order_id (approval_id, proposal_id) VALUES (:a, :p)"),
                         {"a": approval["approval_id"], "p": approval["proposal_id"]})
        await db.commit()
    return {"ok": True}
'''

FKS = {"broker_order_id": {"approval_id": "approval", "proposal_id": "proposal"}}


def _refs(test: str) -> list[str]:
    defs = es.sibling_definitions([{"node_key": "T2", "status": "done", "output_text": "```python\n" + EXECUTOR + "```"}])
    return es.sibling_refs("```python\n" + test + "```", defs, fks=FKS, rules={})


def test_a_call_the_test_expects_to_raise_needs_no_parent_row():
    test = '''# tests/test_paper_twelve_modules.py
import pytest
from app.modules.paper_order_executor import execute_order

async def test_missing_symbol_raises():
    approval = {"proposal_id": 7, "proposal": {"side": "buy"}}
    with pytest.raises(ValueError):
        await execute_order(approval)

async def test_writes():
    approval = {"proposal_id": 7, "proposal": {"symbol": "SPY"}}
    await execute_order(approval)
'''
    found = _refs(test)
    # the raise-wrapped call (line 9) is not refused; the plain call (line 12) still is
    assert found and all(f.startswith("`tests/test_paper_twelve_modules.py:12` calls `execute_order(…)`") for f in found), found
    assert any("is read for `approval_id`" in f for f in found), found


@pytest.mark.asyncio
async def test_the_frontier_claim_is_retried_over_a_slow_connect():
    from app.modules import execution_agent as ea
    calls = {"n": 0}

    class _Session:
        async def __aenter__(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError()
            return AsyncMock()

        async def __aexit__(self, *exc):
            return False

    with patch.object(ea, "async_session", new=lambda: _Session()), \
         patch.object(ea, "_claim_ready_nodes", new=AsyncMock(return_value=[{"node_key": "T5"}])), \
         patch.object(ea, "touch", new=AsyncMock()), \
         patch.object(ea.asyncio, "sleep", new=AsyncMock()) as slept:
        assert await ea._claim_frontier("job", 1, set()) == [{"node_key": "T5"}]
    assert calls["n"] == 3 and slept.await_count == 2
    # three slow connects in a row still fail the run, as before
    calls["n"] = -10
    with patch.object(ea, "async_session", new=lambda: _Session()), \
         patch.object(ea.asyncio, "sleep", new=AsyncMock()), pytest.raises(TimeoutError):
        await ea._claim_frontier("job", 1, set())
