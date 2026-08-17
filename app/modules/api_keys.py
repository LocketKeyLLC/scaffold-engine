"""§17.807 — scoped API-key management (install-time multi-user option).

Backs the ``MULTI_USER_ENABLED`` install profile: named, revocable API keys
stored in the ``api_keys`` table (migration 066). Only the SHA-256 hex digest
of a key is persisted — the raw value is returned once at mint time and never
stored, mirroring how every other credential in the stack is handled.

Semantics are deliberately flat ("named keys, equal access"): a live key —
one with ``revoked_at IS NULL`` — passes auth with the same access as any
other. The master ``scaffold_api_key`` is validated separately in
``app/auth.py`` as the admin/bootstrap key and is NOT stored here.

Used by:
  * ``app/auth.py``     — ``verify_key`` on the request hot path (multi-user on).
  * ``scripts/keyctl.py`` — ``make key-add`` / ``key-list`` / ``key-revoke`` CLI.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Raw keys carry a stable, greppable prefix so operators can tell a scoped
# scaffold key apart from an OpenAI/Anthropic secret at a glance. The entropy
# is 32 url-safe bytes (~43 chars) — same order as the master key's hex-32.
_KEY_PREFIX = "sk-scaffold-"


def hash_key(raw: str) -> str:
    """Return the SHA-256 hex digest used as the stored/looked-up key handle."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_raw_key() -> str:
    """Mint a fresh raw key. Shown once at creation, then only its hash lives."""
    return f"{_KEY_PREFIX}{secrets.token_urlsafe(32)}"


async def add_key(
    session: AsyncSession,
    *,
    label: str,
    owner: str | None = None,
    role: str = "user",
) -> tuple[int, str]:
    """Mint and persist a new key. Returns ``(id, raw_key)``.

    The raw key is returned to the caller for one-time display and is NOT
    recoverable afterwards — only ``hash_key(raw)`` is stored.

    ``role`` (§17.810) is ``'user'`` (default, least-privilege) or ``'admin'``.
    An admin scoped key sees/manages every job, same as the master key; a user
    key is scoped to its own owner. Validated here so a typo fails fast at mint
    time rather than at the DB CHECK constraint.
    """
    if role not in ("admin", "user"):
        raise ValueError(f"role must be 'admin' or 'user', got {role!r}")
    raw = generate_raw_key()
    row = (
        await session.execute(
            text(
                """
                INSERT INTO api_keys (key_hash, label, owner, role)
                VALUES (:key_hash, :label, :owner, :role)
                RETURNING id
                """
            ),
            {"key_hash": hash_key(raw), "label": label, "owner": owner, "role": role},
        )
    ).one()
    await session.commit()
    return int(row.id), raw


async def list_keys(session: AsyncSession, *, include_revoked: bool = False) -> list[dict[str, Any]]:
    """List keys (id/label/owner/role/created_at/revoked_at). Never exposes the hash."""
    where = "" if include_revoked else "WHERE revoked_at IS NULL"
    rows = (
        await session.execute(
            text(
                f"""
                SELECT id, label, owner, role, created_at, revoked_at
                  FROM api_keys
                  {where}
              ORDER BY id
                """
            )
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def revoke_key(
    session: AsyncSession, *, key_id: int | None = None, label: str | None = None
) -> int:
    """Revoke by id or label. Returns the number of live keys revoked.

    Idempotent: already-revoked rows are left untouched (the ``revoked_at IS
    NULL`` guard means a second revoke reports 0).
    """
    if (key_id is None) == (label is None):
        raise ValueError("revoke_key requires exactly one of key_id or label")
    if key_id is not None:
        clause, params = "id = :id", {"id": key_id}
    else:
        clause, params = "label = :label", {"label": label}
    result = await session.execute(
        text(
            f"""
            UPDATE api_keys
               SET revoked_at = now()
             WHERE {clause} AND revoked_at IS NULL
            """
        ),
        params,
    )
    await session.commit()
    return int(result.rowcount or 0)


async def resolve_key(session: AsyncSession, raw: str) -> dict[str, Any] | None:
    """Return ``{id, owner, role}`` for a live (non-revoked) key, else None.

    Auth hot path — a single indexed lookup against the partial
    ``idx_api_keys_live_hash`` index (mig 066). §17.810 promoted the old
    boolean ``verify_key`` to this richer lookup so ``app/auth.py`` can build a
    Principal (identity + role) instead of a pass/fail, without a second query.
    """
    if not raw:
        return None
    row = (
        await session.execute(
            text(
                """
                SELECT id, owner, role
                  FROM api_keys
                 WHERE key_hash = :key_hash AND revoked_at IS NULL
                 LIMIT 1
                """
            ),
            {"key_hash": hash_key(raw)},
        )
    ).mappings().first()
    return dict(row) if row is not None else None


async def verify_key(session: AsyncSession, raw: str) -> bool:
    """Return True iff ``raw`` matches a live (non-revoked) stored key.

    Thin bool wrapper over ``resolve_key`` kept for backward compatibility.
    """
    return await resolve_key(session, raw) is not None
