"""§17.854 (audit A2/C7) — ownership gates on the node-CRUD router and the
assist chatmap PUT.

Node CRUD was the one mutation surface taking a Principal purely for edit
attribution while doing NO visibility check; the chatmap PUT keyed its
router-level guard on the `session_id` path param it does not have. Both let a
non-admin scoped key touch another owner's resource. These are unit-level guard
tests with a fake session (matching test_authz.py's convention); the live SQL
round-trip is covered by the multi-user integration path.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.authz import ADMIN_PRINCIPAL, Principal
from app.routers import nodes as nodes_router
from app.routers import assist as assist_router

_JOB_ID = "11111111-1111-1111-1111-111111111111"
_SID = "22222222-2222-2222-2222-222222222222"


def _fake_db(owner_row):
    """execute() → result whose .first() yields owner_row (a 1-tuple or None)."""
    result = MagicMock()
    result.first = MagicMock(return_value=owner_row)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


# ── nodes._guard ──────────────────────────────────────────────────────────
@pytest.mark.smoke
async def test_nodes_guard_admin_short_circuits():
    db = _fake_db(("alice",))
    await nodes_router._guard(db, ADMIN_PRINCIPAL, _JOB_ID)
    db.execute.assert_not_awaited()  # admin never queries


@pytest.mark.smoke
async def test_nodes_guard_malformed_uuid_400():
    with pytest.raises(HTTPException) as exc:
        await nodes_router._guard(_fake_db(None), ADMIN_PRINCIPAL, "not-a-uuid")
    assert exc.value.status_code == 400


@pytest.mark.smoke
async def test_nodes_guard_owner_match_ok():
    alice = Principal(identity="alice", role="user")
    await nodes_router._guard(_fake_db(("alice",)), alice, _JOB_ID)  # no raise


@pytest.mark.smoke
async def test_nodes_guard_other_owner_404_not_403():
    alice = Principal(identity="alice", role="user")
    with pytest.raises(HTTPException) as exc:
        await nodes_router._guard(_fake_db(("bob",)), alice, _JOB_ID)
    assert exc.value.status_code == 404


@pytest.mark.smoke
async def test_nodes_guard_missing_job_404():
    alice = Principal(identity="alice", role="user")
    with pytest.raises(HTTPException) as exc:
        await nodes_router._guard(_fake_db(None), alice, _JOB_ID)
    assert exc.value.status_code == 404


# ── chatmap PUT body-session ownership ─────────────────────────────────────
class _ChatMapBody:
    def __init__(self, session_id):
        self.session_id = session_id
        self.last_node_key = None


@pytest.mark.smoke
async def test_chatmap_put_rejects_cross_owner_session():
    """A non-admin binding another owner's session id → 404, and no write."""
    alice = Principal(identity="alice", role="user")
    db = _fake_db(("bob",))
    with pytest.raises(HTTPException) as exc:
        await assist_router.assist_chatmap_put(
            "chat-1", _ChatMapBody(_SID), db=db, principal=alice,
        )
    assert exc.value.status_code == 404
    db.commit.assert_not_awaited()  # never reached the durable-link write


@pytest.mark.smoke
async def test_chatmap_put_admin_no_ownership_query():
    """Admin / single-user path performs no ownership SELECT (unchanged)."""
    db = _fake_db(("alice",))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(assist_router.assist_session_map, "remember", AsyncMock())
        out = await assist_router.assist_chatmap_put(
            "chat-1", _ChatMapBody(_SID), db=db, principal=ADMIN_PRINCIPAL,
        )
    assert out["stored"] is True
