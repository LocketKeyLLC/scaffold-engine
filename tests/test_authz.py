"""§17.810 — unit tests for app/authz.py (principal model + ownership helpers).

Pure logic + fake-session coverage of the RBAC primitives: Principal resolution
from a key row, the admin/user visibility rules, the SQL owner-filter fragment,
and the assert_visible 404 gate. The full end-to-end ownership filtering (a user
seeing only their own jobs through the live API) is exercised in the integration
suite against Postgres; here we lock the deterministic pieces.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app import authz
from app.authz import (
    ADMIN_PRINCIPAL,
    Principal,
    assert_visible,
    can_access,
    get_principal,
    owner_filter,
    principal_for_key_row,
    require_admin,
)


# ── Principal / principal_for_key_row ────────────────────────────────────
@pytest.mark.smoke
def test_admin_principal_shape():
    assert ADMIN_PRINCIPAL.is_admin is True
    assert ADMIN_PRINCIPAL.identity == "admin"
    assert ADMIN_PRINCIPAL.key_id is None


@pytest.mark.smoke
def test_principal_for_key_row_owner_tag_is_identity():
    """Owner tag becomes the identity → multiple keys can share one user."""
    p = principal_for_key_row({"id": 3, "owner": "alice", "role": "user"})
    assert p.identity == "alice"
    assert p.role == "user"
    assert p.key_id == 3
    assert not p.is_admin


@pytest.mark.smoke
def test_principal_for_key_row_no_owner_falls_back_to_key_id():
    """An untagged key gets a stable per-key identity, not a shared empty one."""
    p = principal_for_key_row({"id": 9, "owner": None, "role": "user"})
    assert p.identity == "key:9"


@pytest.mark.smoke
def test_principal_for_key_row_blank_owner_falls_back():
    p = principal_for_key_row({"id": 9, "owner": "   ", "role": "user"})
    assert p.identity == "key:9"


@pytest.mark.smoke
def test_principal_for_key_row_admin_role():
    p = principal_for_key_row({"id": 1, "owner": "root", "role": "admin"})
    assert p.is_admin is True


@pytest.mark.smoke
def test_principal_for_key_row_defaults_role_to_user():
    p = principal_for_key_row({"id": 1, "owner": "x", "role": None})
    assert p.role == "user"


# ── can_access ───────────────────────────────────────────────────────────
@pytest.mark.smoke
def test_can_access_admin_sees_everything():
    assert can_access(ADMIN_PRINCIPAL, "alice") is True
    assert can_access(ADMIN_PRINCIPAL, None) is True  # legacy NULL-owner rows too


@pytest.mark.smoke
def test_can_access_user_only_own():
    alice = Principal(identity="alice", role="user")
    assert can_access(alice, "alice") is True
    assert can_access(alice, "bob") is False
    assert can_access(alice, None) is False  # NULL-owner hidden from non-admin


# ── owner_filter ─────────────────────────────────────────────────────────
@pytest.mark.smoke
def test_owner_filter_admin_is_noop():
    clause, params = owner_filter(ADMIN_PRINCIPAL)
    assert clause == ""
    assert params == {}


@pytest.mark.smoke
def test_owner_filter_user_scopes_by_identity():
    alice = Principal(identity="alice", role="user")
    clause, params = owner_filter(alice)
    assert clause == " AND owner = :principal_owner"
    assert params == {"principal_owner": "alice"}


@pytest.mark.smoke
def test_owner_filter_respects_column_and_param_override():
    alice = Principal(identity="alice", role="user")
    clause, params = owner_filter(alice, column="j.owner", param="po")
    assert clause == " AND j.owner = :po"
    assert params == {"po": "alice"}


# ── get_principal / require_admin ────────────────────────────────────────
@pytest.mark.smoke
def test_get_principal_defaults_to_admin_when_unset():
    """Exempt/loopback routes never set request.state.principal → admin default."""
    req = MagicMock()
    req.state = SimpleNamespace()  # no `principal` attribute
    assert get_principal(req) is ADMIN_PRINCIPAL


@pytest.mark.smoke
def test_get_principal_returns_attached():
    alice = Principal(identity="alice", role="user")
    req = MagicMock()
    req.state = SimpleNamespace(principal=alice)
    assert get_principal(req) is alice


@pytest.mark.smoke
def test_require_admin_allows_admin():
    assert require_admin(ADMIN_PRINCIPAL) is ADMIN_PRINCIPAL


@pytest.mark.smoke
def test_require_admin_rejects_user():
    alice = Principal(identity="alice", role="user")
    with pytest.raises(HTTPException) as exc:
        require_admin(alice)
    assert exc.value.status_code == 403


# ── assert_visible (fake session) ────────────────────────────────────────
def _fake_db(row):
    """A stand-in AsyncSession whose execute() returns a result with .first() → row."""
    result = MagicMock()
    result.first = MagicMock(return_value=row)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.smoke
async def test_assert_visible_admin_short_circuits_no_query():
    db = _fake_db(("alice",))
    await assert_visible(db, ADMIN_PRINCIPAL, "some-id")
    db.execute.assert_not_awaited()  # admin never queries


@pytest.mark.smoke
async def test_assert_visible_owner_match_ok():
    alice = Principal(identity="alice", role="user")
    db = _fake_db(("alice",))
    await assert_visible(db, alice, "job-1")  # no raise
    db.execute.assert_awaited_once()


@pytest.mark.smoke
async def test_assert_visible_missing_row_404():
    alice = Principal(identity="alice", role="user")
    db = _fake_db(None)
    with pytest.raises(HTTPException) as exc:
        await assert_visible(db, alice, "job-1", detail="job not found: job-1")
    assert exc.value.status_code == 404
    assert exc.value.detail == "job not found: job-1"


@pytest.mark.smoke
async def test_assert_visible_other_owner_404_not_403():
    """A non-owner gets 404 (opacity), not 403 — the resource's existence is
    not disclosed to a user who doesn't own it."""
    alice = Principal(identity="alice", role="user")
    db = _fake_db(("bob",))
    with pytest.raises(HTTPException) as exc:
        await assert_visible(db, alice, "job-1")
    assert exc.value.status_code == 404
