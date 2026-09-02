"""§17.484 — persistent per-role model overrides.

Makes the §17.483 web "set model per role" durable. The override is held in
the `model_overrides` table (migration 050) AND mirrored onto the live
`settings` singleton so request-path resolution (`config.get_model`) stays a
pure attribute read — no DB hit per model lookup.

Flow:
  - set_override(role, model)   → validate + UPSERT row + mutate settings.<role>
  - clear_override(role)        → DELETE row + revert settings.<role> to env default
  - load_overrides_into_settings() → at lifespan startup, replay stored rows
                                     onto settings (so a restart restores them)
  - list_overrides()            → {role: model} for the rows currently set

`set_runtime_model` / `clear_runtime_model` (app.config) own the validation +
the in-process mutation; this module owns persistence. Keeping them split means
the in-process layer is unit-testable without a DB, and the DB layer reuses the
same role/blank guards.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.config import (
    SWITCHABLE_ROLE_FIELDS,
    clear_runtime_model,
    clear_runtime_provider,
    set_runtime_model,
    set_runtime_provider,
)

logger = logging.getLogger("scaffold.model_overrides")

# §17.900 — the row now carries PROVIDER alongside the model tag. A NULL
# provider means "use the role's configured/default provider", so every row
# written before migration 074 keeps its exact prior behavior.
_UPSERT = text(
    "INSERT INTO model_overrides (role, model, provider, updated_at) "
    "VALUES (:role, :model, :provider, now()) "
    "ON CONFLICT (role) DO UPDATE SET model = EXCLUDED.model, "
    "  provider = EXCLUDED.provider, updated_at = now()"
)
_DELETE = text("DELETE FROM model_overrides WHERE role = :role")
_SELECT_ALL = text("SELECT role, model, provider FROM model_overrides ORDER BY role")


async def set_override(role: str, model: str, db, provider: str | None = None) -> None:
    """Persist a switchable role's override and apply it in-process.

    Validates FIRST (raises ValueError on a non-switchable role / blank tag /
    unknown provider) so we never write an unusable row. The row + both settings
    mutations are committed together.
    """
    set_runtime_model(role, model)          # validates role + non-blank, mutates settings
    p = (provider or "").strip() or None
    if p:
        set_runtime_provider(role, p)       # validates against the registry allowlist
    await db.execute(_UPSERT, {"role": role, "model": model.strip(), "provider": p})
    await db.commit()
    logger.info("model_override_set role=%s model=%s provider=%s (persisted)",
                role, model.strip(), p or "(role default)")


async def clear_override(role: str, db) -> None:
    """Remove a role's persisted override and revert it to the env default."""
    clear_runtime_model(role)               # validates role, reverts settings to env default
    try:
        clear_runtime_provider(role)        # §17.900 — revert the provider too
    except ValueError:
        pass                                # role has no provider field: nothing to revert
    await db.execute(_DELETE, {"role": role})
    await db.commit()
    logger.info("model_override_cleared role=%s (reverted to env default)", role)


async def list_overrides(db) -> dict[str, str]:
    """Return ``{role: model}`` for the rows currently overridden."""
    rows = (await db.execute(_SELECT_ALL)).mappings().all()
    return {r["role"]: r["model"] for r in rows}


async def list_provider_overrides(db) -> dict[str, str]:
    """§17.900 — ``{role: provider}`` for rows that pin a provider."""
    rows = (await db.execute(_SELECT_ALL)).mappings().all()
    return {r["role"]: r["provider"] for r in rows if r.get("provider")}


async def load_overrides_into_settings(db) -> int:
    """Replay every stored override onto the live settings singleton.

    Called once at lifespan startup, AFTER migrations create the table. Returns
    the count applied. Fail-soft per row: a stored role that is no longer
    switchable (config drift) is skipped + logged rather than crashing startup —
    the row stays so the operator can see/clear it.
    """
    applied = 0
    rows = (await db.execute(_SELECT_ALL)).mappings().all()
    for r in rows:
        role, model, provider = r["role"], r["model"], r.get("provider")
        if role not in SWITCHABLE_ROLE_FIELDS:
            logger.warning(
                "model_override_load_skip role=%s (no longer switchable)", role
            )
            continue
        try:
            set_runtime_model(role, model)
            applied += 1
        except ValueError as exc:
            logger.warning("model_override_load_skip role=%s err=%s", role, exc)
            continue
        # §17.900 — replay the provider pin too. Independently fail-soft: a
        # provider that has since been de-registered must not cost the role its
        # (valid) model override.
        if provider:
            try:
                set_runtime_provider(role, provider)
            except ValueError as exc:
                logger.warning("model_override_provider_load_skip role=%s err=%s",
                               role, exc)
    if applied:
        logger.info("model_overrides_loaded count=%d", applied)
    return applied
