"""§17.813 — model-management JSON API (audit M13 / plan Phase 4).

Mounted in ``app/main.py``; inherits the global ``Depends(require_api_key)``.
Writes are additionally admin-gated (global model config is not per-owner).

  GET    /models/roles          — live effective config: every role → model,
                                  source (override | env | default), env default
  PUT    /models/roles/{role}   — set a switchable role's model (persisted via
                                  §17.484 set_override); validates the tag
                                  against the pulled Ollama list and, for
                                  *cloud* tags, runs a generate-probe (the tag
                                  list serves stale 200s for retired cloud
                                  models — the §17.632 liveness gotcha)
  DELETE /models/roles/{role}   — clear the override, revert to env default
  POST   /models/probe          — explicit generate-based liveness probe (the
                                  wizard's "test" button)

This is the JSON surface the §17.812-plan Phase 5 wizard + OWUI ``/model set``
route through — before it, the only write path was the /web HTML form (M13).
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz import require_admin
from app.config import (
    ROLE_FIELDS,
    SWITCHABLE_ROLE_FIELDS,
    env_default_model,
    provider_field_for,
    settings,
    valid_provider_names,
)
from app.database import get_db
from app.modules import provider_connections as pconn
from app.modules.model_overrides import clear_override, list_overrides, set_override

logger = logging.getLogger("scaffold")

router = APIRouter(tags=["Models"])


class RoleModelInput(BaseModel):
    model: str = Field(description="The model tag to point this role at.")
    # §17.900 — a role now names its BACKEND as well as its model. Omitted
    # (None) keeps the role's current provider, so every pre-§17.900 caller
    # (the wizard, OWUI `/model set`) behaves identically.
    provider: str | None = Field(
        default=None,
        description=(
            "Which backend serves this role: ollama | openai | anthropic | "
            "huggingface. Omit to keep the role's current provider."
        ),
    )
    probe: bool = Field(
        default=True,
        description=(
            "Generate-probe cloud tags before applying (the pulled-tag list "
            "serves stale 200s for retired cloud models). Local tags are only "
            "checked against the pulled list — probing one would force a full "
            "model load on this CPU-only host. Applies to the ollama provider "
            "only; remote providers are validated against their /models list."
        ),
    )


class ConnectionInput(BaseModel):
    """§17.900 — one provider's credentials/endpoint."""
    api_key: str | None = Field(
        default=None,
        description=(
            "The API key. Omit to leave any stored key untouched (so the base "
            "URL can be edited without re-typing the secret); pass an empty "
            "string to CLEAR it and revert to the environment value."
        ),
    )
    base_url: str | None = Field(
        default=None, description="Override the provider's endpoint. Omit to keep.")
    enabled: bool = Field(default=True)
    label: str | None = Field(default=None, description="Optional display name.")


class DefaultProviderInput(BaseModel):
    provider: str = Field(
        description="Fallback backend for roles with no explicit provider.")


class ProbeInput(BaseModel):
    model: str = Field(description="The model tag to probe (generate-based).")


def _is_cloud_tag(model: str) -> bool:
    return model.endswith(":cloud") or model.endswith("-cloud")


async def _pulled_tags() -> set[str] | None:
    """The Ollama tag list, or None when unreachable (callers fail-soft —
    an unreachable daemon must not brick model management)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            return {m.get("name", "") for m in (r.json().get("models") or [])}
    except Exception as exc:  # noqa: BLE001 — connection/timeout/parse
        logger.warning("models_api tag-list unreachable: %s", exc)
        return None


async def _generate_probe(model: str, timeout: float = 60.0) -> dict:
    """One direct /api/generate round-trip against ``model`` — deliberately NOT
    through model_router.generate(), whose smart-fallback would mask a dead tag
    by answering from the fallback model. Returns {ok, latency_ms, error}."""
    url = f"{settings.ollama_base_url.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "prompt": "Reply with OK.",
        "stream": False,
        "options": {"num_predict": 8},
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code >= 400:
            return {"ok": False, "latency_ms": latency_ms,
                    "error": f"HTTP {r.status_code}: {r.text[:300]}"}
        return {"ok": True, "latency_ms": latency_ms, "error": None}
    except Exception as exc:  # noqa: BLE001 — surface, don't raise
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "latency_ms": latency_ms, "error": repr(exc)[:300]}


def _role_provider(role: str) -> str:
    """§17.900 — the backend currently serving a role (mirrors the resolution
    in providers.provider_for_role, including the global default)."""
    return (getattr(settings, provider_field_for(role), None)
            or settings.model_default_provider or "ollama")


def _role_row(role: str, overrides: dict[str, str]) -> dict:
    switchable = role in SWITCHABLE_ROLE_FIELDS
    current = getattr(settings, role)
    if not switchable:
        source = "env" if os.environ.get(role.upper()) else "default"
        return {"role": role, "model": current, "source": source,
                "switchable": False, "env_default": current,
                "provider": _role_provider(role), "provider_switchable": False}
    env_def = env_default_model(role)
    if role in overrides:
        source = "override"
    elif os.environ.get(role.upper()):
        source = "env"
    else:
        source = "default"
    return {"role": role, "model": current, "source": source,
            "switchable": True, "env_default": env_def,
            # §17.900 — the UI renders a provider dropdown per role.
            "provider": _role_provider(role), "provider_switchable": True}


@router.get("/models/roles")
async def get_model_roles(db: AsyncSession = Depends(get_db)) -> dict:
    """Live effective model config for every role, with provenance."""
    overrides = await list_overrides(db)
    roles = [_role_row(r, overrides) for r in sorted(ROLE_FIELDS)]
    return {"roles": roles, "switchable": sorted(SWITCHABLE_ROLE_FIELDS)}


@router.get("/models/available")
async def get_available_models(provider: str | None = None) -> dict:
    """§17.817 (plan 5.7) — models the picker can offer.

    §17.900: now provider-aware. Without ``?provider=`` this keeps its original
    Ollama-shaped contract (``local``/``cloud``/``ollama_url``) so the wizard
    and OWUI callers are untouched; with it, the same call answers for any
    connected backend. ``reachable: false`` with empty lists when the backend
    is down — the picker renders that state instead of spinning forever.
    """
    name = (provider or "ollama").strip()
    if name == "ollama":
        tags = await _pulled_tags()
        if tags is None:
            return {"reachable": False, "provider": "ollama",
                    "ollama_url": settings.ollama_base_url,
                    "local": [], "cloud": [], "models": []}
        names = sorted(t for t in tags if t)
        return {
            "reachable": True,
            "provider": "ollama",
            "ollama_url": settings.ollama_base_url,
            "local": [t for t in names if not _is_cloud_tag(t)],
            "cloud": [t for t in names if _is_cloud_tag(t)],
            "models": names,
        }
    if name not in valid_provider_names():
        raise HTTPException(
            status_code=422,
            detail=f"unknown provider {name!r}; must be one of {list(valid_provider_names())}",
        )
    try:
        models = await pconn.list_provider_models(name)
    except Exception as exc:  # noqa: BLE001 — an unconfigured provider is a
        # normal, renderable state, not a server error.
        return {"reachable": False, "provider": name, "models": [],
                "local": [], "cloud": [], "error": pconn._explain(exc)}
    return {"reachable": True, "provider": name, "models": models,
            "local": [], "cloud": models}


# ── §17.900 — provider connections ───────────────────────────────────────────

@router.get("/models/connections")
async def get_connections(db: AsyncSession = Depends(get_db)) -> dict:
    """Every provider with its effective connection state.

    API keys are ALWAYS masked to "(set)"/"(unset)" — there is deliberately no
    endpoint that returns a stored key, so a compromised session cannot
    exfiltrate credentials the operator pasted in."""
    return {
        "connections": await pconn.list_connections(db),
        "providers": list(valid_provider_names()),
        "default_provider": settings.model_default_provider,
    }


@router.put("/models/connections/{provider}", dependencies=[Depends(require_admin)])
async def put_connection(
    provider: str, body: ConnectionInput, db: AsyncSession = Depends(get_db),
) -> dict:
    """Set a provider's credentials/endpoint (encrypted at rest, applied live)."""
    try:
        await pconn.set_connection(
            provider, api_key=body.api_key, base_url=body.base_url,
            enabled=body.enabled, label=body.label, db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"connections": await pconn.list_connections(db)}


@router.delete("/models/connections/{provider}", dependencies=[Depends(require_admin)])
async def delete_connection(
    provider: str, db: AsyncSession = Depends(get_db),
) -> dict:
    """Forget a stored connection; the provider reverts to its env defaults."""
    try:
        await pconn.delete_connection(provider, db)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"connections": await pconn.list_connections(db)}


@router.post("/models/connections/{provider}/test",
             dependencies=[Depends(require_admin)])
async def test_connection(
    provider: str, db: AsyncSession = Depends(get_db),
) -> dict:
    """Live reachability + auth check ("does my key actually work")."""
    if provider not in valid_provider_names():
        raise HTTPException(
            status_code=422,
            detail=f"unknown provider {provider!r}; must be one of {list(valid_provider_names())}",
        )
    return await pconn.test_connection(provider, db)


@router.put("/models/default-provider", dependencies=[Depends(require_admin)])
async def put_default_provider(
    body: DefaultProviderInput, db: AsyncSession = Depends(get_db),
) -> dict:
    """The fallback backend for roles with no explicit provider — the "move
    everything to Claude" switch. Persisted (system_flags), so it survives a
    restart rather than silently reverting to Ollama."""
    try:
        await pconn.set_default_provider(body.provider.strip(), db)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"default_provider": settings.model_default_provider}


@router.put("/models/roles/{role}", dependencies=[Depends(require_admin)])
async def put_model_role(
    role: str, body: RoleModelInput, db: AsyncSession = Depends(get_db),
) -> dict:
    """Point a switchable role at a model (persisted; survives restart)."""
    model = (body.model or "").strip()
    if not model:
        raise HTTPException(status_code=422, detail="model tag is empty")
    if role not in SWITCHABLE_ROLE_FIELDS:
        raise HTTPException(
            status_code=422,
            detail=(f"role {role!r} is not switchable; must be one of "
                    f"{sorted(SWITCHABLE_ROLE_FIELDS)}"),
        )
    # §17.900 — validate against the provider that will actually SERVE this
    # role. The previous gate checked every tag against the pulled Ollama list
    # unconditionally, which made `gpt-5` and `claude-opus-5` unsettable even
    # though both providers already worked — the single blocker behind "connect
    # ChatGPT or Claude".
    provider = (body.provider or "").strip() or _role_provider(role)
    if provider not in valid_provider_names():
        raise HTTPException(
            status_code=422,
            detail=(f"unknown provider {provider!r}; must be one of "
                    f"{list(valid_provider_names())}"),
        )
    if provider == "ollama":
        tags = await _pulled_tags()
        if tags is not None and model not in tags:
            raise HTTPException(
                status_code=422,
                detail=(f"model {model!r} is not a pulled Ollama tag. Pull it "
                        f"first (`ollama pull {model}`) — a GGUF from "
                        f"HuggingFace works too: `ollama pull hf.co/<user>/<repo>`."),
            )
        # Cloud tags list even after retirement (stale-200) — probe on *generate*.
        if body.probe and _is_cloud_tag(model):
            probe = await _generate_probe(model)
            if not probe["ok"]:
                raise HTTPException(
                    status_code=422,
                    detail=f"model {model!r} failed the generate probe: {probe['error']}",
                )
    else:
        # Remote provider: the credential has to exist before we pin a role to
        # it, else the role silently 401s on its next real call. Listing models
        # exercises the URL and the key together.
        try:
            available = await pconn.list_provider_models(provider)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail=(f"cannot reach provider {provider!r}: "
                        f"{pconn._explain(exc)} — connect it in Settings → "
                        f"Connections first."),
            )
        if available and model not in available:
            raise HTTPException(
                status_code=422,
                detail=(f"model {model!r} is not offered by {provider!r}. "
                        f"Available: {', '.join(available[:12])}"
                        f"{'…' if len(available) > 12 else ''}"),
            )
    try:
        await set_override(role, model, db, provider=provider)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    overrides = await list_overrides(db)
    return _role_row(role, overrides)


@router.delete("/models/roles/{role}", dependencies=[Depends(require_admin)])
async def delete_model_role(role: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Clear a role's persisted override; revert to the env/config default."""
    try:
        await clear_override(role, db)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    overrides = await list_overrides(db)
    return _role_row(role, overrides)


@router.post("/models/probe", dependencies=[Depends(require_admin)])
async def probe_model(body: ProbeInput) -> dict:
    """Explicit generate-based liveness probe (the wizard's "test" button).

    Always generates — the caller opted into the latency, and for a LOCAL tag
    that first call is also what a real request would pay (model load).

    §17.858 — slow-box honesty: a successful LOCAL probe additionally carries
    ``slow`` (bool). A first result over ``slow_box_probe_warn_ms`` may just be
    the one-time model load (disk speed ≠ inference speed), so it triggers ONE
    warm re-probe (reported as ``warm_latency_ms``) and the flag sticks only if
    the warm number is also over. A slow probe adds ``slow_threshold_ms`` +
    ``node_timeout_seconds`` so the client can render an honest warning.
    Cloud tags are never assessed — their latency is network, not this box."""
    model = (body.model or "").strip()
    if not model:
        raise HTTPException(status_code=422, detail="model tag is empty")
    result = await _generate_probe(model, timeout=120.0)
    out = {"model": model, **result}
    if result["ok"] and not _is_cloud_tag(model):
        warn_ms = settings.slow_box_probe_warn_ms
        slow = result["latency_ms"] > warn_ms
        if slow:
            warm = await _generate_probe(model, timeout=120.0)
            if warm["ok"]:
                out["warm_latency_ms"] = warm["latency_ms"]
                slow = warm["latency_ms"] > warn_ms
        out["slow"] = slow
        if slow:
            out["slow_threshold_ms"] = warn_ms
            out["node_timeout_seconds"] = settings.node_timeout_seconds
    return out
