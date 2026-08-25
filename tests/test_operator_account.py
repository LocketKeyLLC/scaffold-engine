"""§17.840 — admin account: scrypt hashing, status/setup/login endpoints,
login throttle. Style mirrors test_meta_first_run.py (direct calls, mocked db)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers import operator_account as oa


@pytest.fixture(autouse=True)
def _reset_throttle():
    oa._throttle_reset()
    yield
    oa._throttle_reset()


def _db_scalar(value):
    db = AsyncMock()
    r = MagicMock()
    r.scalar.return_value = value
    db.execute = AsyncMock(return_value=r)
    return db


def _settings_with_key(key: str):
    s = MagicMock()
    s.scaffold_api_key.get_secret_value.return_value = key
    return s


# ── Hashing ──────────────────────────────────────────────────────────────────


def test_hash_verify_roundtrip():
    stored = oa.hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert oa.verify_password("correct horse battery staple", stored)
    assert not oa.verify_password("wrong", stored)


def test_verify_rejects_malformed_stored():
    assert not oa.verify_password("x", "")
    assert not oa.verify_password("x", "md5$nope")
    assert not oa.verify_password("x", "scrypt$bad$fields")


def test_hashes_are_salted_unique():
    assert oa.hash_password("pw-same") != oa.hash_password("pw-same")


# ── /auth/account/status ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_unclaimed():
    out = await oa.account_status(db=_db_scalar(None))
    assert out == {"claimed": False, "display_name": None, "login_available": False}


@pytest.mark.asyncio
async def test_status_claimed_with_master_key():
    acct = {"display_name": "Adam", "password_hash": oa.hash_password("pw123456")}
    with patch.object(oa, "settings", _settings_with_key("sk-live")):
        out = await oa.account_status(db=_db_scalar(acct))
    assert out == {"claimed": True, "display_name": "Adam", "login_available": True}


@pytest.mark.asyncio
async def test_status_claimed_but_no_master_key():
    """§17.807 no-master multi-user install: account may exist, but there is
    no console credential to hand out — login must report unavailable."""
    acct = {"display_name": "Adam", "password_hash": "scrypt$x"}
    with patch.object(oa, "settings", _settings_with_key("")):
        out = await oa.account_status(db=_db_scalar(acct))
    assert out["claimed"] is True and out["login_available"] is False


# ── /auth/account/setup ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_upserts_flag_and_hashes():
    db = AsyncMock()
    body = oa.AccountSetup(display_name="  Adam ", password="pw123456")
    out = await oa.account_setup(body=body, db=db)
    assert out == {"claimed": True, "display_name": "Adam"}
    sql = str(db.execute.call_args.args[0])
    assert "ON CONFLICT" in sql and "system_flags" in sql
    import json

    stored = json.loads(db.execute.call_args.args[1]["v"])
    assert stored["display_name"] == "Adam"
    assert stored["password_hash"].startswith("scrypt$")
    assert "pw123456" not in stored["password_hash"]
    db.commit.assert_awaited_once()


# ── /auth/login ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_unclaimed_404():
    with pytest.raises(HTTPException) as e:
        await oa.login(body=oa.LoginRequest(password="x"), db=_db_scalar(None))
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_login_no_master_409():
    acct = {"display_name": "A", "password_hash": oa.hash_password("pw123456")}
    with patch.object(oa, "settings", _settings_with_key("")):
        with pytest.raises(HTTPException) as e:
            await oa.login(body=oa.LoginRequest(password="pw123456"), db=_db_scalar(acct))
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_login_wrong_password_401_right_password_returns_key():
    acct = {"display_name": "Adam", "password_hash": oa.hash_password("pw123456")}
    with patch.object(oa, "settings", _settings_with_key("sk-live")):
        with pytest.raises(HTTPException) as e:
            await oa.login(body=oa.LoginRequest(password="nope-nope"), db=_db_scalar(acct))
        assert e.value.status_code == 401
        out = await oa.login(body=oa.LoginRequest(password="pw123456"), db=_db_scalar(acct))
    assert out == {"api_key": "sk-live", "display_name": "Adam"}


@pytest.mark.asyncio
async def test_login_throttles_after_five_failures_and_resets_on_success():
    acct = {"display_name": "A", "password_hash": oa.hash_password("pw123456")}
    with patch.object(oa, "settings", _settings_with_key("sk-live")):
        for _ in range(5):
            with pytest.raises(HTTPException) as e:
                await oa.login(body=oa.LoginRequest(password="bad"), db=_db_scalar(acct))
            assert e.value.status_code == 401
        # 6th attempt hits the lock, even with the RIGHT password.
        with pytest.raises(HTTPException) as e:
            await oa.login(body=oa.LoginRequest(password="pw123456"), db=_db_scalar(acct))
        assert e.value.status_code == 429
        # After the lock expires, success resets the counter + lock duration.
        oa._throttle["locked_until"] = 0.0
        out = await oa.login(body=oa.LoginRequest(password="pw123456"), db=_db_scalar(acct))
        assert out["api_key"] == "sk-live"
        assert oa._throttle["failures"] == 0 and oa._throttle["lock_secs"] == 30.0


# ── Auth exemption wiring ────────────────────────────────────────────────────


def test_exempt_paths_cover_login_and_status_but_not_setup():
    from app.auth import _AUTH_EXEMPT_PATHS

    assert "/auth/login" in _AUTH_EXEMPT_PATHS
    assert "/auth/account/status" in _AUTH_EXEMPT_PATHS
    assert "/auth/account/setup" not in _AUTH_EXEMPT_PATHS
