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
from app.modules import assist_handoff


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
    # 'researching' is neither a valid live-start status nor a terminal re-open
    # status (§17.623), so it is still rejected. ('completed' now re-opens — see
    # test_start_session_reopens_completed_job.)
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "id": "abc", "status": "researching",
            "job_type": "legacy", "node_count": 5,
        }),
    ]
    with pytest.raises(ValueError, match="assist mode requires"):
        await assist_agent.start_assist_session(
            job_id="11111111-1111-1111-1111-111111111111", db=db,
        )


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_session_reopens_completed_job():
    """§17.623 — /assist on a 'completed' job re-opens it for a hands-on redo:
    it resets DAG nodes + assist_steps to pending and returns reopened=True,
    instead of the old confusing "already completed" 409."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "id": "job-9", "status": "completed",
            "job_type": "component", "node_count": 7,
        }),
        _result(mappings_first={
            "id": "sess-9", "job_id": "job-9", "status": "active",
            "handoff_policy": "manual", "replan_policy": "context_only",
        }),
        _result(),                          # UPDATE dag_nodes reset (re-open)
        _result(),                          # UPDATE jobs status
        _result(),                          # INSERT seed assist_steps
        _result(),                          # UPDATE assist_steps reset (re-open)
        _result(),                          # §17.681 UPDATE assist_sessions → active
        _result(scalar=7),                  # SELECT total
        _result(scalar=7),                  # SELECT pending
    ]
    out = await assist_agent.start_assist_session(
        job_id="99999999-9999-9999-9999-999999999999", db=db,
    )
    assert out["reopened"] is True
    # §17.681 — reopened sessions always report 'active' (the ON CONFLICT return
    # carried the stale terminal status before the forced reset).
    assert out["status"] == "active"
    assert out["pending_steps"] == 7
    # §17.681 — the session status reset must have run (else a reopened session
    # keeps a terminal status and get_next_step yields nothing).
    sess_reset = [
        str(c.args[0]) for c in db.execute.await_args_list
        if c.args and "assist_sessions" in str(c.args[0])
        and "status = 'active'" in str(c.args[0])
    ]
    assert sess_reset, "re-open did not reset the assist session to active"
    assert db.commit.await_count == 1
    # The dag_nodes reset must have run — assert one UPDATE targeted dag_nodes
    # back to pending.
    reset_sqls = [
        str(c.args[0]) for c in db.execute.await_args_list
        if c.args and "dag_nodes" in str(c.args[0]) and "'pending'" in str(c.args[0])
    ]
    assert reset_sqls, "re-open did not reset dag_nodes to pending"


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
        _result(mappings_first={
            "id": "job-1", "status": "planning",
            "job_type": "legacy", "node_count": 4,
        }),
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
async def test_start_session_on_awaiting_assist_seeds_directly():
    """§17.624 — a job parked in 'awaiting_assist' by the hands-on gate starts
    assist WITHOUT a re-open reset (nodes are already pending): reopened is
    False and the normal seed path runs (no dag_nodes/assist_steps reset)."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "id": "job-h", "status": "awaiting_assist",
            "job_type": "component", "node_count": 7,
        }),
        _result(mappings_first={
            "id": "sess-h", "job_id": "job-h", "status": "active",
            "handoff_policy": "manual", "replan_policy": "context_only",
        }),
        _result(),                          # UPDATE jobs status
        _result(),                          # INSERT seed assist_steps
        _result(scalar=7),                  # SELECT total
        _result(scalar=7),                  # SELECT pending
    ]
    out = await assist_agent.start_assist_session(
        job_id="77777777-7777-7777-7777-777777777777", db=db,
    )
    assert out["reopened"] is False
    assert out["pending_steps"] == 7
    assert db.commit.await_count == 1
    # No dag_nodes RESET update should have run (that's the re-open path only).
    # The seed INSERT ... SELECT FROM dag_nodes is expected; the reset is an
    # `UPDATE dag_nodes SET status = 'pending'`.
    assert not [
        c for c in db.execute.await_args_list
        if c.args and "UPDATE dag_nodes" in str(c.args[0])
        and "SET status = 'pending'" in str(c.args[0])
    ]
    # §17.625 regression — the job-status transition UPDATE must list
    # 'awaiting_assist' in its WHERE IN-list, else a parked job never enters
    # assisted_executing and _maybe_finalize_session can never mark it
    # 'completed' at the end of the walkthrough.
    transition = [
        c for c in db.execute.await_args_list
        if c.args and "SET status = 'assisted_executing'" in str(c.args[0])
    ]
    assert transition, "no job→assisted_executing transition UPDATE issued"
    assert "awaiting_assist" in str(transition[0].args[0])


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_session_seeds_environment_from_sibling():
    """§17.723 — a NEW component session inherits the environment (facts /
    substitutions / profile) of its most recently active sibling session under
    the same umbrella: the components run against the same physical system, so
    a new component must not start blind."""
    sibling_env = {
        "profile": "root@pve single shell",
        "facts": ["ZFS pool 'oasis' active"],
        "substitutions": {"POOL_NAME": "oasis"},
    }
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "id": "job-c2", "status": "awaiting_assist",
            "job_type": "component", "node_count": 3,
        }),
        _result(mappings_first={
            "id": "sess-c2", "job_id": "job-c2", "status": "active",
            "handoff_policy": "manual", "replan_policy": "context_only",
            "inserted": True,
        }),
        _result(mappings_first={"env": sibling_env}),  # sibling env lookup
        _result(),                          # UPDATE session metadata (seed)
        _result(),                          # UPDATE jobs status
        _result(),                          # INSERT seed assist_steps
        _result(scalar=3),                  # SELECT total
        _result(scalar=3),                  # SELECT pending
    ]
    out = await assist_agent.start_assist_session(
        job_id="55555555-5555-5555-5555-555555555555", db=db,
    )
    assert out["session_id"] == "sess-c2"
    seed_updates = [
        c for c in db.execute.await_args_list
        if c.args and "metadata" in str(c.args[0])
        and len(c.args) > 1 and "oasis" in str(c.args[1])
    ]
    assert seed_updates, "sibling environment was not seeded into the new session"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_session_existing_session_does_not_seed():
    """§17.723 — reconnecting to an EXISTING session (ON CONFLICT path,
    inserted=False) must never overwrite the environment it already gathered."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "id": "job-c3", "status": "assisted_executing",
            "job_type": "component", "node_count": 3,
        }),
        _result(mappings_first={
            "id": "sess-c3", "job_id": "job-c3", "status": "active",
            "handoff_policy": "manual", "replan_policy": "context_only",
            "inserted": False,
        }),
        _result(),                          # UPDATE jobs status
        _result(),                          # INSERT seed assist_steps
        _result(scalar=3),                  # SELECT total
        _result(scalar=3),                  # SELECT pending
    ]
    out = await assist_agent.start_assist_session(
        job_id="66666666-6666-6666-6666-666666666666", db=db,
    )
    assert out["session_id"] == "sess-c3"
    assert not [
        c for c in db.execute.await_args_list
        if c.args and "parent_job_id" in str(c.args[0])
    ], "sibling env lookup ran for an existing session"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_session_umbrella_returns_assist_unavailable():
    """§17.561 — /assist on an umbrella job returns structured guidance, not a
    phantom empty session. No INSERT/UPDATE, no commit; child rollup attached."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "id": "u-1", "status": "aggregating",
            "job_type": "umbrella", "node_count": 0,
        }),
        _result(mappings_all=[
            {"id": "c-1", "title": "Backend", "status": "running",
             "component_index": 0},
            {"id": "c-2", "title": "Frontend", "status": "completed",
             "component_index": 1},
        ]),
    ]
    out = await assist_agent.start_assist_session(
        job_id="33333333-3333-3333-3333-333333333333", db=db,
    )
    assert out["assist_unavailable"] is True
    assert out["reason"] == "umbrella"
    assert out["children_total"] == 2
    assert out["children"][0]["title"] == "Backend"
    db.commit.assert_not_called()
    assert db.execute.await_count == 2  # SELECT job + SELECT children only


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_session_zero_node_job_returns_assist_unavailable():
    """A non-umbrella job with 0 DAG nodes also gets the friendly guard
    (reason='no_dag') instead of an empty session."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={
            "id": "j-0", "status": "planning",
            "job_type": "legacy", "node_count": 0,
        }),
    ]
    out = await assist_agent.start_assist_session(
        job_id="44444444-4444-4444-4444-444444444444", db=db,
    )
    assert out["assist_unavailable"] is True
    assert out["reason"] == "no_dag"
    assert out["children"] == []
    db.commit.assert_not_called()
    assert db.execute.await_count == 1  # only the SELECT job ran


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
_CLAIMED_T1 = {"id": "n1", "node_key": "T1", "title": "Node T1"}


class _FakeSession:
    """Module-level async_session() stand-in that records execute/commit.

    Serves all three async_session() blocks in single-mode handoff: the
    job->'executing' flip, the scoped node claim (reads .mappings().first()),
    and the restore-to-'assisted_executing' (reads .scalar())."""

    def __init__(self, rec, claimed=_CLAIMED_T1, scalar_value="active"):
        self._rec = rec
        self._claimed = claimed
        # Value returned by .scalar() — the restore reads the session status
        # ("active" to proceed) and the §17.599 finalize reads the job status
        # ("completed" to finalize the session).
        self._scalar_value = scalar_value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self._rec.append(("execute", str(stmt), params))
        r = MagicMock()
        r.scalar.return_value = self._scalar_value
        r.mappings.return_value.first.return_value = self._claimed  # claim row
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


def _fake_session_factory(rec, claimed=_CLAIMED_T1):
    return lambda: _FakeSession(rec, claimed)


@pytest.mark.asyncio
async def test_handoff_single_runs_only_one_node(monkeypatch):
    """§17.594 — single handoff must execute EXACTLY the claimed node via the
    per-node executor, and must NOT fall through to execute_all_nodes (which
    drains the whole remaining DAG — the original bug)."""
    rec: list = []
    monkeypatch.setattr(
        assist_handoff, "async_session", _fake_session_factory(rec),
    )

    all_nodes_called = {"hit": False}

    async def _fake_exec_all(job_id):
        all_nodes_called["hit"] = True
        yield 'event: node\ndata: {}\n\n'

    next_calls: list = []

    async def _fake_exec_next(job_id, preclaimed_node=None):
        next_calls.append((job_id, preclaimed_node))
        return {
            "status": "done", "node_key": preclaimed_node["node_key"],
            "title": preclaimed_node["title"], "verified": True,
            "job_complete": False,
        }

    monkeypatch.setattr(
        "app.modules.execution_agent.execute_all_nodes", _fake_exec_all,
    )
    monkeypatch.setattr(
        "app.modules.execution_agent.execute_next_node", _fake_exec_next,
    )

    events = [
        ev async for ev in assist_agent.handoff_step(
            session_id="sess-1", node_key="T1", mode="single", db=_handoff_db(),
        )
    ]

    assert all_nodes_called["hit"] is False, \
        "single handoff drained the whole DAG via execute_all_nodes (§17.594)"
    assert len(next_calls) == 1, "single handoff must run exactly one node"
    assert next_calls[0][1]["node_key"] == "T1"
    # The claim is scoped to the target node_key only.
    claim = [c for c in rec if c[0] == "execute" and "node_key = :nk" in c[1]]
    assert claim and claim[0][2]["nk"] == "T1"
    assert any("node_done" in e for e in events)
    assert any("assist_handoff_done" in e for e in events)


@pytest.mark.asyncio
async def test_handoff_single_noop_when_node_not_pending(monkeypatch):
    """§17.594 — if the target node isn't 'pending' (already ran), the claim
    returns nothing: emit a no-op and never invoke the executor."""
    rec: list = []
    monkeypatch.setattr(
        assist_handoff, "async_session", _fake_session_factory(rec, claimed=None),
    )

    called = {"next": False}

    async def _fake_exec_next(job_id, preclaimed_node=None):
        called["next"] = True
        return {"status": "done"}

    monkeypatch.setattr(
        "app.modules.execution_agent.execute_next_node", _fake_exec_next,
    )

    events = [
        ev async for ev in assist_agent.handoff_step(
            session_id="sess-1", node_key="T1", mode="single", db=_handoff_db(),
        )
    ]
    assert called["next"] is False
    assert any("assist_handoff_noop" in e for e in events)


@pytest.mark.asyncio
async def test_handoff_all_remaining_uses_execute_all_nodes(monkeypatch):
    """all_remaining mode still delegates the whole rest of the DAG."""
    rec: list = []
    monkeypatch.setattr(
        assist_handoff, "async_session", _fake_session_factory(rec),
    )

    all_called = {"hit": False}

    async def _fake_exec_all(job_id):
        all_called["hit"] = True
        yield 'event: node\ndata: {}\n\n'

    next_called = {"hit": False}

    async def _fake_exec_next(job_id, preclaimed_node=None):
        next_called["hit"] = True
        return {"status": "done"}

    monkeypatch.setattr(
        "app.modules.execution_agent.execute_all_nodes", _fake_exec_all,
    )
    monkeypatch.setattr(
        "app.modules.execution_agent.execute_next_node", _fake_exec_next,
    )

    events = [
        ev async for ev in assist_agent.handoff_step(
            session_id="sess-1", node_key="T1", mode="all_remaining",
            db=_handoff_db(),
        )
    ]
    assert all_called["hit"] is True
    assert next_called["hit"] is False
    assert any("assist_handoff_done" in e for e in events)


@pytest.mark.asyncio
async def test_handoff_finalizes_session_when_job_completes(monkeypatch):
    """§17.599 — when the handoff drives the job to 'completed', the assist
    session is transitioned out of 'active' so /assist/_chatmap stops routing
    plain chat into a done session and the idle reaper doesn't mislabel it."""
    rec: list = []
    monkeypatch.setattr(
        assist_handoff, "async_session",
        lambda: _FakeSession(rec, scalar_value="completed"),
    )

    async def _fake_exec_all(job_id):
        yield 'event: node\ndata: {}\n\n'

    monkeypatch.setattr(
        "app.modules.execution_agent.execute_all_nodes", _fake_exec_all,
    )

    events = [
        ev async for ev in assist_agent.handoff_step(
            session_id="sess-1", node_key="T1", mode="all_remaining",
            db=_handoff_db(),
        )
    ]
    assert any("assist_handoff_done" in e for e in events)
    finalize = [
        c for c in rec
        if c[0] == "execute" and "assist_sessions" in c[1]
        and "'completed'" in c[1]
    ]
    assert finalize, "session was not finalized to 'completed' after job complete"


@pytest.mark.asyncio
async def test_handoff_does_not_finalize_when_job_not_complete(monkeypatch):
    """§17.599 — a single-node handoff that leaves the job running must NOT
    finalize the session (the operator continues in assist)."""
    rec: list = []
    monkeypatch.setattr(
        assist_handoff, "async_session",
        lambda: _FakeSession(rec, scalar_value="active"),
    )

    async def _fake_exec_next(job_id, preclaimed_node=None):
        return {"status": "done", "node_key": "T1", "title": "T1",
                "verified": True, "job_complete": False}

    monkeypatch.setattr(
        "app.modules.execution_agent.execute_next_node", _fake_exec_next,
    )

    async for _ in assist_agent.handoff_step(
        session_id="sess-1", node_key="T1", mode="single", db=_handoff_db(),
    ):
        pass
    finalize = [
        c for c in rec
        if c[0] == "execute" and "assist_sessions" in c[1]
        and "'completed'" in c[1]
    ]
    assert not finalize, "session finalized despite job not complete"


@pytest.mark.asyncio
async def test_handoff_single_restores_assist_mode(monkeypatch):
    rec: list = []
    monkeypatch.setattr(
        assist_handoff, "async_session", _fake_session_factory(rec),
    )

    async def _fake_exec_next(job_id, preclaimed_node=None):
        return {
            "status": "done", "node_key": "T1", "title": "Node T1",
            "verified": True, "job_complete": False,
        }

    monkeypatch.setattr(
        "app.modules.execution_agent.execute_next_node", _fake_exec_next,
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
    # §17.410 — the finally restore is asyncio.shield-wrapped; a client
    # disconnect mid-handoff (now during the per-node executor) must NOT
    # leave the job stranded in 'executing'.
    rec: list = []
    monkeypatch.setattr(
        assist_handoff, "async_session", _fake_session_factory(rec),
    )

    async def _fake_exec_next(job_id, preclaimed_node=None):
        await asyncio.sleep(10)  # block so we can cancel mid-execution
        return {"status": "done"}

    monkeypatch.setattr(
        "app.modules.execution_agent.execute_next_node", _fake_exec_next,
    )

    async def _drive():
        async for _ in assist_agent.handoff_step(
            session_id="sess-1", node_key="T1", mode="single", db=_handoff_db(),
        ):
            pass

    task = asyncio.create_task(_drive())
    await asyncio.sleep(0.05)  # let it reach the blocking executor call
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Give the shielded restore a tick to complete on the loop.
    await asyncio.sleep(0.1)
    restore = [c for c in rec if c[0] == "execute" and "assisted_executing" in c[1]]
    assert restore, "shielded restore did not run under cancellation (E1 regression)"
    assert any(c[0] == "commit" for c in rec)


# ── §17.645 — one step in flight at a time (get_next_step) ──────────────────


@pytest.mark.asyncio
async def test_get_next_step_represents_inflight_before_claiming(monkeypatch):
    """When a step is already presented-but-unsubmitted, `next` re-presents THAT
    instead of claiming a new (possibly far) node — no claim UPDATE runs."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={"id": "s1", "job_id": "j1", "status": "active"}),
        # §17.699 — the divergence-notice metadata SELECT (no proposal staged).
        _result(mappings_first={"metadata": None}),
    ]
    inflight = {"node_key": "T1", "re_presented": True}
    monkeypatch.setattr(assist_agent, "_load_presented_step",
                        AsyncMock(return_value=inflight))
    res = await assist_agent.get_next_step(session_id="s1", db=db)
    assert res is inflight
    assert db.execute.await_count == 2  # session SELECT + notice SELECT; no claim


@pytest.mark.asyncio
async def test_get_next_step_claims_when_nothing_inflight(monkeypatch):
    """With nothing in flight, it proceeds to the claim path (here nothing is
    claimable → falls through to the None fallback)."""
    db = AsyncMock()
    db.execute.side_effect = [
        _result(mappings_first={"id": "s1", "job_id": "j1", "status": "active"}),
        # §17.699 — the divergence-notice metadata SELECT (no proposal staged).
        _result(mappings_first={"metadata": None}),
        _result(mappings_first=None),  # claim UPDATE → nothing claimable
    ]
    monkeypatch.setattr(assist_agent, "_load_presented_step",
                        AsyncMock(return_value=None))
    res = await assist_agent.get_next_step(session_id="s1", db=db)
    assert res is None
    assert db.execute.await_count == 3  # session SELECT + notice SELECT + claim
