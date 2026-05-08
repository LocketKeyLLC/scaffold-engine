"""Unit-level smoke tests for app/modules/assist_agent.py.

These tests use AsyncMock DB sessions to verify the SQL choreography
(transition guards, idempotency, status flow). End-to-end correctness
is covered by tests/integration/test_assist_flow.py against a real
Postgres.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import assist_agent


def _result(rowcount: int = 0, mappings_first=None, mappings_all=None, scalar=None):
    """Build a SQLAlchemy-result-like mock with mappings()/scalar() shims."""
    r = MagicMock()
    r.rowcount = rowcount
    mappings = MagicMock()
    mappings.first.return_value = mappings_first
    mappings.all.return_value = mappings_all or []
    r.mappings.return_value = mappings
    r.scalar.return_value = scalar
    fetched = MagicMock()
    fetched.fetchall.return_value = []
    r.fetchall = fetched.fetchall
    return r


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_session_rejects_unknown_job():
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first=None),  # SELECT jobs WHERE id=...
    ]
    with pytest.raises(ValueError, match="job not found"):
        await assist_agent.start_assist_session(
            job_id="00000000-0000-0000-0000-000000000000", db=db,
        )


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_session_rejects_invalid_status():
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={"id": "abc", "status": "completed"}),
    ]
    with pytest.raises(ValueError, match="assist mode requires"):
        await assist_agent.start_assist_session(job_id="abc", db=db)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_session_returns_session_dict_and_commits():
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={"id": "job-1", "status": "planning"}),
        _result(mappings_first={
            "id": "sess-1", "job_id": "job-1", "status": "active",
            "handoff_policy": "manual", "replan_policy": "context_only",
        }),
        _result(),                          # UPDATE jobs status
        _result(),                          # INSERT seed assist_steps
        _result(scalar=4),                  # SELECT total
        _result(scalar=4),                  # SELECT pending
    ]
    out = await assist_agent.start_assist_session(job_id="job-1", db=db)
    assert out["session_id"] == "sess-1"
    assert out["total_steps"] == 4
    assert out["pending_steps"] == 4
    assert db.commit.await_count == 1


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_submit_step_rejects_invalid_action():
    db = AsyncMock()
    with pytest.raises(ValueError, match="action must be"):
        await assist_agent.submit_step(
            session_id="s1", node_key="T1",
            evidence="x", action="explode", db=db,
        )


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_submit_step_requires_evidence_for_submit():
    db = AsyncMock()
    with pytest.raises(ValueError, match="non-empty evidence"):
        await assist_agent.submit_step(
            session_id="s1", node_key="T1",
            evidence="", action="submit", db=db,
        )


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_submit_step_idempotent_on_already_committed():
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "step_id": "x", "status": "committed",
            "session_id": "s1", "job_id": "j1", "node_key": "T1",
        }),
    ]
    out = await assist_agent.submit_step(
        session_id="s1", node_key="T1",
        evidence="anything", action="submit", db=db,
    )
    assert out["no_op"] is True
    assert out["status"] == "committed"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_submit_step_rejects_pending_step():
    """Pending step → structured `must_claim_first:` ValueError so the
    router can map it to a 409 the OWUI pipeline detects."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "step_id": "x", "status": "pending",
            "session_id": "s1", "job_id": "j1", "node_key": "T1",
        }),
    ]
    with pytest.raises(ValueError, match=r"^must_claim_first:"):
        await assist_agent.submit_step(
            session_id="s1", node_key="T1",
            evidence="x", action="submit", db=db,
        )


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_submit_step_rejects_non_claimable_non_pending():
    """Statuses other than pending/committed/skipped fall to the generic
    rejection (e.g., 'applied' before commit). Must NOT use the
    must_claim_first prefix — that's reserved for pending."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "step_id": "x", "status": "applied",
            "session_id": "s1", "job_id": "j1", "node_key": "T1",
        }),
    ]
    with pytest.raises(ValueError, match="cannot accept submit") as exc:
        await assist_agent.submit_step(
            session_id="s1", node_key="T1",
            evidence="x", action="submit", db=db,
        )
    assert "must_claim_first" not in str(exc.value)
