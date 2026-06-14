"""Unit-level smoke tests for app/modules/assist_agent.py.

These tests use AsyncMock DB sessions to verify the SQL choreography
(transition guards, idempotency, status flow). End-to-end correctness
is covered by tests/integration/test_assist_flow.py against a real
Postgres.
"""
from __future__ import annotations

import asyncio
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
        await assist_agent.start_assist_session(
            job_id="11111111-1111-1111-1111-111111111111", db=db,
        )


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_session_rejects_non_uuid_job_id():
    """§17.521 — a non-UUID job_id (e.g. a pasted title) is rejected with a
    clean ValueError BEFORE the query, not a raw asyncpg DataError → HTTP 500."""
    db = AsyncMock()
    with pytest.raises(ValueError, match="not a job id"):
        await assist_agent.start_assist_session(job_id="DeFruscio", db=db)
    db.execute.assert_not_called()  # never reaches the uuid-cast query


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
    out = await assist_agent.start_assist_session(
        job_id="22222222-2222-2222-2222-222222222222", db=db,
    )
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


# ---------------------------------------------------------------------------
# §17.410 — handoff_step (mode='single') restore must survive cancellation.
# The restore to 'assisted_executing' runs in a finally wrapped in
# asyncio.shield; a client disconnect mid-handoff must NOT leave the job
# stranded in 'executing'.
# ---------------------------------------------------------------------------
class _FakeSession:
    """Module-level async_session() stand-in that records execute/commit."""

    def __init__(self, rec):
        self._rec = rec

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self._rec.append(("execute", str(stmt), params))
        r = MagicMock()
        r.scalar.return_value = "active"  # db3 SELECT sees an active session
        return r

    async def commit(self):
        self._rec.append(("commit", None, None))


def _handoff_db():
    """The injected `db`: SELECT active session, then UPDATE handed_off."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={"id": "sess-1", "job_id": "job-1"}),
        _result(),
    ]
    return db


@pytest.mark.asyncio
async def test_handoff_single_restores_assist_mode(monkeypatch):
    rec: list = []
    monkeypatch.setattr(assist_agent, "async_session", lambda: _FakeSession(rec))

    async def _fake_exec_all(job_id):
        yield 'event: node\ndata: {}\n\n'

    monkeypatch.setattr(
        "app.modules.execution_agent.execute_all_nodes", _fake_exec_all,
    )

    events = [
        ev async for ev in assist_agent.handoff_step(
            session_id="sess-1", node_key="T1", mode="single", db=_handoff_db(),
        )
    ]
    assert any("assist_handoff_done" in e for e in events)
    restore = [c for c in rec if c[0] == "execute" and "assisted_executing" in c[1]]
    assert restore, "happy-path restore to assisted_executing did not run"
    assert any(c[0] == "commit" for c in rec)


@pytest.mark.asyncio
async def test_handoff_single_restore_survives_cancellation(monkeypatch):
    rec: list = []
    monkeypatch.setattr(assist_agent, "async_session", lambda: _FakeSession(rec))

    async def _fake_exec_all(job_id):
        yield 'event: node\ndata: {}\n\n'
        await asyncio.sleep(10)  # block so we can cancel mid-stream

    monkeypatch.setattr(
        "app.modules.execution_agent.execute_all_nodes", _fake_exec_all,
    )

    async def _drive():
        async for _ in assist_agent.handoff_step(
            session_id="sess-1", node_key="T1", mode="single", db=_handoff_db(),
        ):
            pass

    task = asyncio.create_task(_drive())
    await asyncio.sleep(0.05)  # let it reach the blocking execute_all_nodes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Give the shielded restore a tick to complete on the loop.
    await asyncio.sleep(0.1)
    restore = [c for c in rec if c[0] == "execute" and "assisted_executing" in c[1]]
    assert restore, "shielded restore did not run under cancellation (E1 regression)"
    assert any(c[0] == "commit" for c in rec)
