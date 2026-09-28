"""§17.1193 — named values the engine holds on the operator's behalf.

§17.1191 refused to hold a password at all: the operator had to write it into
a file on the target machine by hand. That was stricter than the tools this
engine is measured against — Ansible Vault, GitHub Actions secrets, Jenkins
credentials, Vault — every one of which stores the value centrally, encrypted,
and injects it at run time. It also pushed a manual file edit onto the operator
for something a form does everywhere else.

The property §17.1191 actually bought is separable from where the value lives:

    the value must never enter the command string, the approval it is signed
    with, the runner's log line, or the target's process table.

That is delivered by sending it OUT OF BAND to the runner, which binds it to a
`$NAME` reference in the approved command and injects it as an environment
variable — exactly GitHub Actions' model. So the engine may hold it, and the
operator may simply be asked.

At rest: Fernet, the same derivation as `provider_connections` (see
`app/utils/secrets.py`). The plaintext leaves this module only on the path to
a runner — never into an API response, a transcript, a prompt or a log line.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("scaffold")

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(str(name or "")))


async def set_secret(db: AsyncSession, name: str, value: str, *,
                     runner: Optional[str] = None, hint: str = "", owner: Optional[str] = None) -> dict:
    """Store (or replace) one value. Returns the row WITHOUT the value."""
    from app.utils.secrets import encrypt
    if not valid_name(name):
        raise ValueError("a secret name is A-Z, 0-9 and _ , starting with a letter")
    if not str(value or "").strip():
        raise ValueError("a secret needs a value")
    await db.execute(text("""
        INSERT INTO runner_secrets (name, value_enc, runner, hint, owner)
        VALUES (:n, :v, :r, :h, :o)
        ON CONFLICT (name) DO UPDATE
           SET value_enc = EXCLUDED.value_enc, runner = EXCLUDED.runner,
               hint = EXCLUDED.hint, updated_at = NOW()
    """), {"n": name, "v": encrypt(str(value)), "r": runner, "h": (hint or "")[:200], "o": owner})
    await db.commit()
    logger.warning("runner_secret_set name=%s runner=%s", name, runner)   # the NAME, never the value
    return {"name": name, "runner": runner, "hint": hint}


async def list_secrets(db: AsyncSession) -> list[dict]:
    """Names and metadata only — this never returns a value."""
    rows = (await db.execute(text(
        "SELECT name, runner, hint, created_at, updated_at, last_used_at "
        "FROM runner_secrets ORDER BY name"))).mappings().all()
    return [dict(r) for r in rows]


async def names(db: AsyncSession) -> list[str]:
    return [r["name"] for r in await list_secrets(db)]


async def delete_secret(db: AsyncSession, name: str) -> bool:
    res = await db.execute(text("DELETE FROM runner_secrets WHERE name = :n"), {"n": name})
    await db.commit()
    logger.warning("runner_secret_deleted name=%s rows=%s", name, res.rowcount)
    return bool(res.rowcount)


async def values_for(db: AsyncSession, wanted: list[str]) -> dict[str, str]:
    """``{NAME: value}`` for the names asked for — the ONLY path plaintext takes.

    Callers hand this straight to the runner's ``env`` argument; it must not be
    logged, returned to a client, put in a prompt, or written to a node's
    output. A name whose ciphertext cannot be decrypted (the derivation secret
    was rotated) is omitted, so the run refuses on a missing value rather than
    running with an empty one.
    """
    from app.utils.secrets import decrypt
    if not wanted:
        return {}
    rows = (await db.execute(
        text("SELECT name, value_enc FROM runner_secrets WHERE name = ANY(:ns)"),
        {"ns": list(wanted)})).mappings().all()
    out: dict[str, str] = {}
    for r in rows:
        v = decrypt(r["value_enc"])
        if v:
            out[r["name"]] = v
        else:
            logger.error("runner_secret_undecryptable name=%s — the derivation secret changed; re-enter it", r["name"])
    if out:
        await db.execute(text("UPDATE runner_secrets SET last_used_at = NOW() WHERE name = ANY(:ns)"),
                         {"ns": list(out)})
        await db.commit()
    return out
