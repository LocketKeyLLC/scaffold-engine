"""§17.1953 — a CodeGen step that keeps getting the same refusal on the coder escalates to the HEAVY model: the coder is
the cheap first draw (2026-11-19: coder and general both deepseek-v4.1-flash), the heavy one is paid for only by the
steps that need it. A step pinned to another model still goes to the coder first."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import engine_plan, node_editor

_ROLES = {"model_coder": "flash", "model_general": "flash", "model_cloud_heavy": "pro"}
_REFUSAL = ("§17.1749 this step writes code for the engine itself: `tests/test_x.py:4` replaces `write_pnl` "
            "(`paper_audit_trail_writer`, a finished module that never leaves the engine) with a fake")


def _db(assigned: str | None):
    row = {"id": "n1", "tool": "CodeGen", "assigned_model": assigned, "tool_config": {}, "max_retries": 3}
    first = MagicMock(mappings=MagicMock(return_value=MagicMock(first=MagicMock(return_value=row))))
    reasons = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[_REFUSAL, _REFUSAL]))))
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[first, reasons])
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("assigned,to", [(None, "pro"), ("flash", "pro"), ("kimi", "flash")])
async def test_the_escalation_order(monkeypatch, assigned, to):
    monkeypatch.setattr("app.config.get_model", lambda role, _o=None: _ROLES[role])
    edit = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(node_editor, "edit_node", edit)
    out = await engine_plan.switch_code_model(_db(assigned), "j", "T6")
    assert out is not None and out["to"] == to, out
    assert edit.await_args.args[2]["assigned_model"] == to
