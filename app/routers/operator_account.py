"""§17.840 — the operator admin account (password-unlocks-console).

A friendliness layer over the existing key auth, operator-decided in the UI
design phase: one admin account (display name + scrypt password hash) stored
as a ``system_flags`` row (mig 070 — no new table). Logging in with the
password hands the browser the SAME credential it stores today (the master
key), so nothing about key auth changes for the CLI/pipelines/curl.

Security posture (operator-picked):
- ``POST /auth/account/setup`` requires an ALREADY-authenticated admin —
  account creation happens inside the first-run walkthrough after the
  bootstrap pairing link signed the browser in. No first-visitor-claims race.
- ``POST /auth/login`` and ``GET /auth/account/status`` are auth-exempt
  (added to ``_AUTH_EXEMPT_PATHS`` — exact paths, so the authed setup
  endpoint stays protected). Login is throttled in-process: 5 consecutive
  failures lock it for 30s, doubling up to 5 min (single-worker uvicorn, so
  a module-level counter is the whole story).
- In multi-user installs running WITHOUT a master key (§17.807) there is no
  console credential to hand out — login reports unavailable (409).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz import require_admin
from app.config import settings
from app.database import get_db

router = APIRouter(tags=["Auth"])

_FLAG = "operator_account"

# scrypt parameters — interactive-login grade (~50ms on this CPU class).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt_b64$hash_b64` — self-describing so parameters can
    be raised later without invalidating stored hashes."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
        )
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


async def _get_account(db: AsyncSession) -> dict | None:
    row = (await db.execute(
        text("SELECT value FROM system_flags WHERE key = :k"), {"k": _FLAG},
    )).scalar()
    return row if isinstance(row, dict) else None


def _login_available() -> bool:
    """No master key (multi-user §17.807) → no console credential to hand out."""
    return bool(settings.scaffold_api_key.get_secret_value())


# ── Login throttle (module state; single-worker uvicorn) ─────────────────────
_throttle = {"failures": 0, "locked_until": 0.0, "lock_secs": 30.0}
_LOCK_AFTER = 5
_LOCK_MAX_SECS = 300.0


def _throttle_check() -> None:
    remaining = _throttle["locked_until"] - time.monotonic()
    if remaining > 0:
        raise HTTPException(429, f"Too many attempts — retry in {int(remaining) + 1}s")


def _throttle_fail() -> None:
    _throttle["failures"] += 1
    if _throttle["failures"] >= _LOCK_AFTER:
        _throttle["locked_until"] = time.monotonic() + _throttle["lock_secs"]
        _throttle["lock_secs"] = min(_throttle["lock_secs"] * 2, _LOCK_MAX_SECS)
        _throttle["failures"] = 0


def _throttle_reset() -> None:
    _throttle.update(failures=0, locked_until=0.0, lock_secs=30.0)


# ── Endpoints ────────────────────────────────────────────────────────────────
class AccountSetup(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@router.get("/auth/account/status")
async def account_status(db: AsyncSession = Depends(get_db)) -> dict:
    """Public (auth-exempt): does an admin account exist, and can the gate
    offer password login? The display name is shown on the sign-in page
    ("Welcome back, X") — deliberate, same disclosure class as OWUI/Grafana."""
    acct = await _get_account(db)
    return {
        "claimed": acct is not None,
        "display_name": (acct or {}).get("display_name"),
        "login_available": acct is not None and _login_available(),
    }


@router.post("/auth/account/setup", dependencies=[Depends(require_admin)])
async def account_setup(body: AccountSetup, db: AsyncSession = Depends(get_db)) -> dict:
    """Create/replace the admin account. Admin-authed by design: reaching
    this proves the caller already holds the key, so nobody races to claim."""
    import json

    value = json.dumps({
        "display_name": body.display_name.strip(),
        "password_hash": hash_password(body.password),
    })
    await db.execute(
        text("""
            INSERT INTO system_flags (key, value, updated_at)
            VALUES (:k, CAST(:v AS jsonb), now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """),
        {"k": _FLAG, "v": value},
    )
    await db.commit()
    return {"claimed": True, "display_name": body.display_name.strip()}


@router.post("/auth/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Password → console credential (the master key, exactly what the paste
    gate stores). Auth-exempt; throttled; 404 unclaimed / 409 no-master."""
    _throttle_check()
    acct = await _get_account(db)
    if acct is None:
        raise HTTPException(404, "No admin account exists — sign in with your API key")
    if not _login_available():
        raise HTTPException(409, "Password login unavailable (no master key configured)")
    if not verify_password(body.password, acct.get("password_hash") or ""):
        _throttle_fail()
        raise HTTPException(401, "Wrong password")
    _throttle_reset()
    return {
        "api_key": settings.scaffold_api_key.get_secret_value(),
        "display_name": acct.get("display_name"),
    }
