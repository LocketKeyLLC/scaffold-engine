"""§17.1941 — before the delivery question opens, every finished review's findings have their fix steps (the pass a
review runs when it finishes). Live (5b4880c0, 2026-11-04 and 11-05): twice the API audits finished with NOT MET
findings, no fix step opened, and the run parked at DELIVER."""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import engine_plan, lab_together
from app.modules import execution_agent as ea


class _Session:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_a_review_left_without_its_fixes_gets_them_and_the_run_goes_back(monkeypatch):
    apply = AsyncMock(return_value={"inserted": ["API_FINRA_AUDITF1"], "edited": ["DELIVER"]})
    monkeypatch.setattr(engine_plan, "apply_to_job", apply)
    monkeypatch.setattr(lab_together, "async_session", lambda: _Session())
    assert await lab_together.review_fix_steps("j1") == ["API_FINRA_AUDITF1"]
    assert apply.await_args.kwargs["only_reviews"] is True
    assert lab_together.take_reset("j1") is True


@pytest.mark.asyncio
async def test_nothing_left_to_fix_adds_nothing(monkeypatch):
    monkeypatch.setattr(engine_plan, "apply_to_job", AsyncMock(return_value={"inserted": []}))
    monkeypatch.setattr(lab_together, "async_session", lambda: _Session())
    assert await lab_together.review_fix_steps("j2") == []
    assert lab_together.take_reset("j2") is False


def test_the_delivery_question_runs_it_first():
    src = inspect.getsource(ea._pause_for_decision)
    assert src.index("await review_fix_steps(job_id)") < src.index("await stale_test_steps(")
