"""§17.1923 — a code step that used its attempts while converging gets another round (fresh attempts, what passed
kept), at most twice; a step that is not converging does not.

Live (5b4880c0, 2026-11-05 03:4x UTC): API_ALPACA_AUDITF3's round ran 5 -> 7 -> 5 -> 3 failing (19 passing) and the
step was marked failed; API_ALPACA_AUDITF1's ran 21 -> 24 -> 17 -> 22 failing (2 passing) -- the first deserves
another round, the second another approach."""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import engine_plan as ep
from app.modules import execution_retry as er


def test_the_sandbox_count_line_is_read():
    assert ep.sandbox_counts("…\nFAILED tests/x.py::t - x\n3 failed, 19 passed in 2.42s") == (3, 19)
    assert ep.sandbox_counts("1 failed, 23 errors in 2.55s") == (24, 0)
    assert ep.sandbox_counts("19 passed, 5 errors in 2.63s") == (5, 19)
    assert ep.sandbox_counts("12 passed in 1.0s") == (0, 12)
    assert ep.sandbox_counts("§17.1749 this step writes code … reaches for things") is None


def _db(reasons, cfg=None):
    row = {"id": "n1", "tool": "CodeGen", "tool_config": cfg or {}, "max_retries": 3}
    r1 = MagicMock(); r1.mappings.return_value.first.return_value = row
    r2 = MagicMock(); r2.scalars.return_value.all.return_value = list(reversed(reasons))    # newest first, as queried
    db = MagicMock(); db.execute = AsyncMock(side_effect=[r1, r2])
    return db


@pytest.mark.asyncio
async def test_a_converging_round_is_extended_and_one_that_is_not_is_left_failed():
    conv = ["5 failed, 17 passed in 1s", "7 failed, 15 passed in 1s", "5 failed, 17 passed in 1s", "3 failed, 19 passed in 1s"]
    with patch("app.modules.node_editor.edit_node", new=AsyncMock(return_value={"status": "ok"})) as ed:
        got = await ep.extend_converging_step(_db(conv), "j", "F3")
    assert got == {"node_key": "F3", "round": 1, "runs": [(5, 17), (7, 15), (5, 17), (3, 19)]}
    assert ed.await_args.args[2]["tool_config"]["converging_rounds"] == 1
    flat = ["21 failed, 3 passed in 1s", "24 errors in 1s", "17 failed, 7 passed in 1s", "22 failed, 2 passed in 1s"]
    with patch("app.modules.node_editor.edit_node", new=AsyncMock()) as ed2:
        assert await ep.extend_converging_step(_db(flat), "j", "F1") is None
        assert await ep.extend_converging_step(_db(conv, {"converging_rounds": 2}), "j", "F3") is None   # capped
        assert await ep.extend_converging_step(_db(["§17.1749 refused", "§17.1749 refused"]), "j", "F3") is None
    ed2.assert_not_awaited()


def test_the_retry_path_tries_it_after_the_model_switch():
    src = inspect.getsource(er.retry_failed_node)
    assert src.index("switch_code_model(") < src.index("extend_converging_step(") < src.index("split_oversized_step(")
