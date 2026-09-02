"""§17.900 — runtime provider connections (credentials + endpoints).

Before this, connecting ChatGPT or Claude meant editing `.env` and restarting
the container: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and the base URLs were
env-only. The providers themselves already worked — the gap was purely that
nothing could configure them at runtime.

This module owns the `provider_connections` table and mirrors it onto the live
`settings` singleton, exactly as `model_overrides` (§17.484) does for model
tags, so the request path stays a pure attribute read with no DB hit.

Layering, deliberately the same split as model_overrides:
  * `app.utils.secrets`  — encrypt/decrypt/mask, no DB, no settings
  * this module          — persistence + the settings mirror + liveness tests
  * `app.routers.models` — HTTP shape and authz

Two invariants worth stating because breaking either is silent and bad:

1. **A key never leaves the process in plaintext.** Reads return
   ``secrets.mask(...)`` — "(set)" / "(unset)" — matching /config's redaction
   vocabulary (§17.611). There is no endpoint that echoes a stored key back.
2. **A NULL row/field means "fall back to env".** An install that has always
   configured OPENAI_API_KEY in .env keeps working with no row at all, and
   deleting a connection reverts to the env value rather than blanking it.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.utils import secrets

logger = logging.getLogger("scaffold.provider_connections")

# The providers a connection row may configure, mapped to the settings fields
# they drive and the shared httpx client that must be rebuilt when the base URL
# changes. `ollama` is included: it has no API key, but its base URL is very
# much something an operator retargets (a different host, a remote box).
PROVIDER_FIELDS: dict[str, dict[str, Any]] = {
    "ollama": {
        "key_field": None,                       # no credential
        "url_field": "ollama_base_url",
        "client": "ollama",
        "label": "Ollama (local / self-hosted)",
        "key_hint": "",
        "default_url": "http://172.18.0.1:11434",
    },
    "openai": {
        "key_field": "openai_api_key",
        "url_field": "openai_base_url",
        "client": "openai",
        "label": "OpenAI (ChatGPT)",
        "key_hint": "sk-…",
        "default_url": "https://api.openai.com/v1",
    },
    "anthropic": {
        "key_field": "anthropic_api_key",
        "url_field": "anthropic_base_url",
        "client": "anthropic",
        "label": "Anthropic (Claude)",
        "key_hint": "sk-ant-…",
        "default_url": "https://api.anthropic.com/v1",
    },
    "huggingface": {
        "key_field": "huggingface_api_key",
        "url_field": "huggingface_base_url",
        "client": "hf_inference",
        "label": "HuggingFace (hosted inference)",
        "key_hint": "hf_…",
        "default_url": "https://router.huggingface.co/v1",
    },
}

_UPSERT = text(
    "INSERT INTO provider_connections "
    "  (provider, api_key_enc, base_url, enabled, label, updated_at) "
    "VALUES (:provider, :key, :url, :enabled, :label, now()) "
    "ON CONFLICT (provider) DO UPDATE SET "
    "  api_key_enc = COALESCE(EXCLUDED.api_key_enc, provider_connections.api_key_enc), "
    "  base_url    = COALESCE(EXCLUDED.base_url,    provider_connections.base_url), "
    "  enabled     = EXCLUDED.enabled, "
    "  label       = COALESCE(EXCLUDED.label, provider_connections.label), "
    "  updated_at  = now()"
)
_CLEAR_KEY = text(
    "UPDATE provider_connections SET api_key_enc = NULL, updated_at = now() "
    "WHERE provider = :provider"
)
_DELETE = text("DELETE FROM provider_connections WHERE provider = :provider")
_SELECT_ALL = text(
    "SELECT provider, api_key_enc, base_url, enabled, label, last_ok_at, last_error "
    "FROM provider_connections ORDER BY provider"
)
_MARK = text(
    "INSERT INTO provider_connections (provider, last_ok_at, last_error, updated_at) "
    "VALUES (:provider, :ok_at, :err, now()) "
    "ON CONFLICT (provider) DO UPDATE SET "
    "  last_ok_at = EXCLUDED.last_ok_at, last_error = EXCLUDED.last_error, "
    "  updated_at = now()"
)


def known_providers() -> list[str]:
    return list(PROVIDER_FIELDS.keys())


# The env-loaded values of every provider field, captured BEFORE any stored
# connection is mirrored on top of them.
#
# Both "clear the key" and "forget the connection" are documented as reverting
# to the ENVIRONMENT value — and `Settings.model_fields[f].default` is NOT that:
# it is the hardcoded default in config.py. Using it would blank an
# OPENAI_API_KEY that .env had legitimately supplied, turning "undo my UI
# change" into "break the install's original config".
_ENV_BASELINE: dict[str, Any] = {}


def capture_env_baseline() -> None:
    """Snapshot the env-loaded provider fields. Idempotent; must run before
    `load_connections_into_settings` mirrors anything on top."""
    if _ENV_BASELINE:
        return
    from app.config import settings
    for spec in PROVIDER_FIELDS.values():
        for field in (spec["key_field"], spec["url_field"]):
            if field and field not in _ENV_BASELINE:
                _ENV_BASELINE[field] = getattr(settings, field, None)


def _env_value(field: str):
    """The env-loaded value for a settings field, else its config default."""
    if field in _ENV_BASELINE:
        return _ENV_BASELINE[field]
    from app.config import Settings
    return Settings.model_fields[field].default


def _apply_to_settings(provider: str, *, api_key: str | None, base_url: str | None) -> None:
    """Mirror one connection onto the live settings singleton.

    None means "leave whatever env/config already provides" — this is what makes
    a partially-configured row (URL only, or key only) behave sanely.
    """
    from pydantic import SecretStr

    from app.config import settings
    spec = PROVIDER_FIELDS.get(provider)
    if not spec:
        return
    if api_key is not None and spec["key_field"]:
        setattr(settings, spec["key_field"], SecretStr(api_key))
    if base_url is not None and spec["url_field"]:
        setattr(settings, spec["url_field"], base_url)
    # The client bakes base_url in at construction, so retarget it now or every
    # later call still goes to the old endpoint (§17.900).
    if base_url is not None and spec["client"]:
        try:
            from app.utils.http_clients import rebuild_client
            rebuild_client(spec["client"])
        except Exception as exc:  # noqa: BLE001 — never fail a write on this
            logger.warning("provider_client_rebuild_failed provider=%s err=%r",
                           provider, exc)


async def set_connection(
    provider: str, *, api_key: str | None = None, base_url: str | None = None,
    enabled: bool = True, label: str | None = None, db,
) -> None:
    """Persist a provider connection and apply it in-process.

    ``api_key=None`` leaves any stored key untouched (so an operator can edit
    the URL without re-typing the secret); ``api_key=""`` explicitly CLEARS it,
    reverting that provider to its env value.
    """
    if provider not in PROVIDER_FIELDS:
        raise ValueError(
            f"unknown provider {provider!r}; must be one of {known_providers()}")
    spec = PROVIDER_FIELDS[provider]
    if api_key is not None and not spec["key_field"]:
        raise ValueError(f"provider {provider!r} takes no API key")

    enc: str | None = None
    clearing_key = api_key is not None and not api_key.strip()
    if api_key is not None and api_key.strip():
        enc = secrets.encrypt(api_key.strip())

    await db.execute(_UPSERT, {
        "provider": provider,
        "key": enc,                                    # NULL → COALESCE keeps the old one
        "url": (base_url or "").strip() or None,
        "enabled": bool(enabled),
        "label": (label or "").strip() or None,
    })
    if clearing_key:
        await db.execute(_CLEAR_KEY, {"provider": provider})
    await db.commit()

    if clearing_key:
        # Documented behavior: clearing reverts to the ENV value, not to blank.
        from app.config import settings
        setattr(settings, spec["key_field"], _env_value(spec["key_field"]))
        _apply_to_settings(provider, api_key=None,
                           base_url=(base_url or "").strip() or None)
    else:
        _apply_to_settings(
            provider,
            api_key=(api_key.strip() if api_key else None),
            base_url=(base_url or "").strip() or None,
        )
    logger.info(
        "provider_connection_set provider=%s key=%s url=%s enabled=%s",
        provider, "cleared" if clearing_key else ("set" if enc else "unchanged"),
        (base_url or "").strip() or "unchanged", bool(enabled),
    )


async def delete_connection(provider: str, db) -> None:
    """Drop a connection row; the provider reverts to its env/config defaults."""
    if provider not in PROVIDER_FIELDS:
        raise ValueError(
            f"unknown provider {provider!r}; must be one of {known_providers()}")
    await db.execute(_DELETE, {"provider": provider})
    await db.commit()
    # Revert settings to the values Pydantic loaded from env at boot — NOT to
    # config.py's hardcoded defaults, which would blank a legitimately
    # .env-supplied key (see _ENV_BASELINE).
    from app.config import settings
    spec = PROVIDER_FIELDS[provider]
    for field in (spec["key_field"], spec["url_field"]):
        if field:
            setattr(settings, field, _env_value(field))
    if spec["client"]:
        try:
            from app.utils.http_clients import rebuild_client
            rebuild_client(spec["client"])
        except Exception:  # noqa: BLE001
            pass
    logger.info("provider_connection_deleted provider=%s (reverted to env)", provider)


async def list_connections(db) -> list[dict]:
    """Every known provider with its CURRENT effective state.

    Always returns a row per provider (not just stored ones) so the UI can
    render "not connected" without inventing entries, and reports where the
    credential actually comes from — `db`, `env`, or nothing.
    """
    from app.config import settings
    stored = {r["provider"]: r for r in
              (await db.execute(_SELECT_ALL)).mappings().all()}
    out: list[dict] = []
    for name, spec in PROVIDER_FIELDS.items():
        row = stored.get(name) or {}
        db_key = secrets.decrypt(row.get("api_key_enc")) if row.get("api_key_enc") else None
        env_key = None
        if spec["key_field"]:
            raw = getattr(settings, spec["key_field"], None)
            env_key = raw.get_secret_value() if hasattr(raw, "get_secret_value") else raw
        effective_key = db_key or env_key
        out.append({
            "provider": name,
            "label": row.get("label") or spec["label"],
            "requires_key": bool(spec["key_field"]),
            "key_hint": spec["key_hint"],
            # NEVER the value — see module docstring invariant 1.
            "api_key": secrets.mask(effective_key),
            "key_source": ("db" if db_key else "env" if env_key else "none"),
            # A stored ciphertext we can no longer read (rotated secret) must be
            # visible, not silently ignored: the operator has to re-enter it.
            "key_unreadable": bool(row.get("api_key_enc")) and db_key is None,
            "base_url": (row.get("base_url")
                         or getattr(settings, spec["url_field"], "")
                         or spec["default_url"]),
            "default_url": spec["default_url"],
            "enabled": bool(row.get("enabled", True)),
            "configured": bool(effective_key) or not spec["key_field"],
            "last_ok_at": row.get("last_ok_at").isoformat() if row.get("last_ok_at") else None,
            "last_error": row.get("last_error"),
        })
    return out


async def load_connections_into_settings(db) -> int:
    """Replay stored connections onto settings at lifespan startup.

    Mirrors `model_overrides.load_overrides_into_settings`. Fail-soft per row: a
    provider that no longer exists, or a ciphertext that no longer decrypts, is
    logged and skipped rather than crashing boot — the row stays so the operator
    can see and fix it in the UI.
    """
    capture_env_baseline()   # BEFORE anything is mirrored on top (see above)
    applied = 0
    try:
        rows = (await db.execute(_SELECT_ALL)).mappings().all()
    except Exception as exc:  # noqa: BLE001 — pre-migration boot must not die
        logger.warning("provider_connections_load_skipped err=%r", exc)
        return 0
    for r in rows:
        name = r["provider"]
        if name not in PROVIDER_FIELDS:
            logger.warning("provider_connection_load_skip provider=%s (unknown)", name)
            continue
        key = secrets.decrypt(r["api_key_enc"]) if r["api_key_enc"] else None
        if r["api_key_enc"] and key is None:
            logger.warning(
                "provider_connection_key_unreadable provider=%s — using env value; "
                "re-enter it in Settings → Connections", name)
        _apply_to_settings(name, api_key=key, base_url=r["base_url"])
        applied += 1
    if applied:
        logger.info("provider_connections_loaded count=%d", applied)
    return applied


# §17.900 — the global default provider is durable state, not a per-process
# knob: an operator who moves the engine onto Claude must not silently land
# back on Ollama at the next restart. Stored in `system_flags` (mig 070) rather
# than a new table, matching first_run_completed / the operator account flag.
_DEFAULT_FLAG = "model_default_provider"
_FLAG_GET = text("SELECT value FROM system_flags WHERE key = :k")
_FLAG_SET = text(
    "INSERT INTO system_flags (key, value, updated_at) "
    "VALUES (:k, :v, now()) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
)


async def set_default_provider(provider: str, db) -> None:
    """Persist the fallback provider and apply it in-process."""
    from app.config import settings, valid_provider_names
    import json
    if provider not in valid_provider_names():
        raise ValueError(
            f"unknown provider {provider!r}; must be one of {list(valid_provider_names())}")
    await db.execute(_FLAG_SET, {"k": _DEFAULT_FLAG,
                                 "v": json.dumps({"provider": provider})})
    await db.commit()
    settings.model_default_provider = provider
    logger.info("model_default_provider_set provider=%s (persisted)", provider)


async def load_default_provider(db) -> str | None:
    """Replay the stored default onto settings at startup. Fail-soft."""
    from app.config import settings, valid_provider_names
    try:
        row = (await db.execute(_FLAG_GET, {"k": _DEFAULT_FLAG})).scalar()
    except Exception as exc:  # noqa: BLE001 — pre-migration boot must not die
        logger.warning("default_provider_load_skipped err=%r", exc)
        return None
    if not row:
        return None
    value = row if isinstance(row, dict) else {}
    name = (value or {}).get("provider")
    if name in valid_provider_names():
        settings.model_default_provider = name
        logger.info("model_default_provider_loaded provider=%s", name)
        return name
    logger.warning("default_provider_load_skip provider=%r (unknown)", name)
    return None


async def test_connection(provider: str, db=None) -> dict:
    """Live reachability + auth check. Returns ``{ok, detail, models}``.

    This is the "does my key actually work" button. It performs the cheapest
    real call each backend offers — a model listing — because that exercises
    the base URL AND the credential together. A 401 therefore reads as a bad
    key rather than as generic unreachability, which is the distinction an
    operator needs.
    """
    if provider not in PROVIDER_FIELDS:
        return {"ok": False, "detail": f"unknown provider {provider!r}", "models": []}
    try:
        models = await list_provider_models(provider)
        ok, detail = True, f"reachable — {len(models)} model(s) available"
    except Exception as exc:  # noqa: BLE001 — every failure is a report, not a raise
        ok, detail, models = False, _explain(exc), []
    if db is not None:
        try:
            from datetime import datetime, timezone
            await db.execute(_MARK, {
                "provider": provider,
                "ok_at": datetime.now(timezone.utc) if ok else None,
                "err": None if ok else detail[:500],
            })
            await db.commit()
        except Exception:  # noqa: BLE001 — recording the result must not fail it
            logger.warning("provider_connection_mark_failed provider=%s", provider)
    logger.info("provider_connection_test provider=%s ok=%s", provider, ok)
    return {"ok": ok, "detail": detail, "models": models}


def _explain(exc: Exception) -> str:
    """Turn a transport/HTTP failure into something an operator can act on."""
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"HTTP {code} — the API key was rejected. Check the key and try again."
        if code == 404:
            return f"HTTP {code} — endpoint not found. Check the base URL."
        return f"HTTP {code}: {exc.response.text[:180]}"
    if isinstance(exc, httpx.ConnectError):
        return f"cannot reach the endpoint ({exc}). Check the base URL and network."
    if isinstance(exc, httpx.TimeoutException):
        return "timed out contacting the endpoint."
    return f"{type(exc).__name__}: {exc}"


async def list_provider_models(provider: str) -> list[str]:
    """The model names a provider can serve right now.

    Each backend advertises differently, so this is the one place that knows
    the per-provider shape; callers (the picker, the test button) stay generic.
    Raises on failure so `test_connection` can classify the error.
    """
    from app.config import settings
    if provider == "ollama":
        from app.utils.http_clients import get_ollama_client
        r = await get_ollama_client().get("/api/tags")
        r.raise_for_status()
        return sorted(m.get("name", "") for m in (r.json() or {}).get("models", []) if m.get("name"))

    if provider == "anthropic":
        from app.providers.anthropic import AnthropicProvider
        from app.utils.http_clients import get_anthropic_client
        r = await get_anthropic_client().get(
            "/models", headers=AnthropicProvider._auth_headers())
        r.raise_for_status()
        return sorted(m.get("id", "") for m in (r.json() or {}).get("data", []) if m.get("id"))

    if provider in ("openai", "huggingface"):
        if provider == "openai":
            from app.providers.openai import OpenAIProvider as P
        else:
            from app.providers.huggingface import HuggingFaceProvider as P
        r = await P._client().get("/models", headers=P._auth_headers())
        r.raise_for_status()
        return sorted(m.get("id", "") for m in (r.json() or {}).get("data", []) if m.get("id"))

    raise ValueError(f"unknown provider {provider!r}")
