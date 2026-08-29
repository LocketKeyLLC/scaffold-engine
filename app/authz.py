"""§17.810 — request principal + basic RBAC / ownership helpers.

Turns the flat "named keys, equal access" auth of §17.807 into a two-tier
identity model:

  * A **Principal** (identity + role) is resolved from the presented key in
    ``app/auth.py`` and attached to ``request.state.principal``.
  * ``get_principal`` is the dependency handlers use to read it; ``require_admin``
    gates admin-only endpoints; ``owner_filter`` / ``visibility_where`` build the
    SQL that scopes job (and job-derived) rows to their owner.

Identity model (see the design decision recorded with §17.810): a key's ``owner``
tag *is* the user, so several keys can map to one person ("multi users per api
keys"). A key with no owner tag falls back to a per-key identity, ``key:<id>``.

Gating: everything here only bites when a **non-admin** principal reaches a
handler, and non-admin principals only exist when ``MULTI_USER_ENABLED`` is on
(single-user installs resolve every request to the admin principal). So the
ownership predicates are a no-op for single-user installs — zero behavior change.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The identity stamped on jobs created by the master/admin key (and by any
# admin-role scoped key that has no owner tag). Admins bypass the owner
# predicate entirely, so this value is only ever the *written* owner, never a
# filter target — but keeping it stable makes admin-created rows self-describing.
ADMIN_IDENTITY = "admin"

ROLE_ADMIN = "admin"
ROLE_USER = "user"
VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_USER})


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. ``identity`` is the ownership key; ``role``
    drives RBAC. ``key_id`` is the api_keys row id for a scoped key (None for
    the master key / auth-disabled)."""

    identity: str
    role: str = ROLE_USER
    key_id: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


# Shared singleton for the trusted/implicit paths: the master key, auth-disabled
# mode, and exempt/loopback routes (see get_principal). Frozen + reused so it can
# be compared by identity in tests.
ADMIN_PRINCIPAL = Principal(identity=ADMIN_IDENTITY, role=ROLE_ADMIN, key_id=None)


def principal_for_key_row(row: dict) -> Principal:
    """Build a Principal from an ``api_keys`` row (id/owner/role).

    Owner tag is the identity when set (so multiple keys → one user); otherwise
    fall back to a stable per-key identity so an untagged key is still isolated
    from every other key rather than colliding on a shared empty owner.
    """
    owner = (row.get("owner") or "").strip()
    key_id = row.get("id")
    identity = owner or f"key:{key_id}"
    role = row.get("role") or ROLE_USER
    return Principal(identity=identity, role=role, key_id=key_id)


def get_principal(request: Request) -> Principal:
    """Dependency: the current request's Principal.

    ``require_api_key`` attaches ``request.state.principal`` for every gated
    request. Exempt/loopback routes (/health, /ui/*) and the
    auth-disabled path never set it — those are trusted operator/probe surfaces,
    so they resolve to the admin principal (full visibility), preserving the
    pre-§17.810 behavior for the native console and health checks.
    """
    return getattr(request.state, "principal", ADMIN_PRINCIPAL)


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    """Dependency for admin-only endpoints (global reaper, key management, …)."""
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required for this operation",
        )
    return principal


def can_access(principal: Principal, owner: str | None) -> bool:
    """In-Python visibility check for an already-fetched row.

    Admin sees everything (incl. NULL-owner legacy rows). A non-admin sees only
    rows whose owner exactly matches their identity; NULL-owner rows are hidden
    from non-admins.
    """
    if principal.is_admin:
        return True
    return owner is not None and owner == principal.identity


def owner_filter(
    principal: Principal, *, column: str = "owner", param: str = "principal_owner"
) -> tuple[str, dict]:
    """Return ``(sql_fragment, params)`` scoping a query to the principal's rows.

    - Admin → ``("", {})`` — no restriction.
    - Non-admin → ``(" AND <column> = :<param>", {param: identity})``.

    ``column`` may be table-qualified (e.g. ``"j.owner"``). The fragment always
    begins with `` AND `` so it can be appended to an existing WHERE clause; use
    ``visibility_where`` when the query has no WHERE yet.
    """
    if principal.is_admin:
        return "", {}
    return f" AND {column} = :{param}", {param: principal.identity}


async def assert_visible(
    db: AsyncSession,
    principal: Principal,
    row_id,
    *,
    table: str = "jobs",
    id_col: str = "id",
    owner_col: str = "owner",
    detail: str = "resource not found",
) -> None:
    """Raise 404 unless ``principal`` may see row ``row_id`` in ``table``.

    A single owner lookup used by mutation/read endpoints whose SQL doesn't
    already carry the owner predicate (cancel/resume/costs/…, which delegate to
    helpers). Admin short-circuits with no query. A non-admin who is missing the
    row OR isn't its owner gets an identical 404, so the endpoint never leaks
    the existence of another user's resource (opacity — 404, not 403).

    ``table``/``id_col``/``owner_col`` are always code-supplied constants (never
    user input), so the f-string interpolation is injection-safe; ``row_id``
    flows through a bind parameter.
    """
    if principal.is_admin:
        return
    row = (
        await db.execute(
            text(f"SELECT {owner_col} AS owner FROM {table} WHERE {id_col} = :rid"),
            {"rid": row_id},
        )
    ).first()
    if row is None or not can_access(principal, row[0]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


async def assert_visible_by_query(
    db: AsyncSession,
    principal: Principal,
    owner_sql: str,
    params: dict,
    *,
    detail: str = "resource not found",
) -> None:
    """Raise 404 unless ``principal`` may see the resource whose owner is returned
    by ``owner_sql`` (first column = owner).

    For resources that don't store ``owner`` directly but derive it from a parent
    (e.g. ``assist_sessions`` → ``jobs.owner`` via ``job_id``). ``owner_sql`` is
    always a code-supplied query string; user values flow through ``params``.
    """
    if principal.is_admin:
        return
    row = (await db.execute(text(owner_sql), params)).first()
    if row is None or not can_access(principal, row[0]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
