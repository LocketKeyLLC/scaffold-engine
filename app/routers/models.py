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
    settings,
)
from app.database import get_db
from app.modules.model_overrides import clear_override, list_overrides, set_override

logger = logging.getLogger("scaffold")

router = APIRouter(tags=["Models"])


class RoleModelInput(BaseModel):
    model: str = Field(description="The model tag to point this role at.")
    probe: bool = Field(
        default=True,
        description=(
            "Generate-probe cloud tags before applying (the pulled-tag list "
            "serves stale 200s for retired cloud models). Local tags are only "
            "checked against the pulled list — probing one would force a full "
            "model load on this CPU-only host."
        ),
    )


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


def _role_row(role: str, overrides: dict[str, str]) -> dict:
    switchable = role in SWITCHABLE_ROLE_FIELDS
    current = getattr(settings, role)
    if not switchable:
        source = "env" if os.environ.get(role.upper()) else "default"
        return {"role": role, "model": current, "source": source,
                "switchable": False, "env_default": current}
    env_def = env_default_model(role)
    if role in overrides:
        source = "override"
    elif os.environ.get(role.upper()):
        source = "env"
    else:
        source = "default"
    return {"role": role, "model": current, "source": source,
            "switchable": True, "env_default": env_def}


@router.get("/models/roles")
async def get_model_roles(db: AsyncSession = Depends(get_db)) -> dict:
    """Live effective model config for every role, with provenance."""
    overrides = await list_overrides(db)
    roles = [_role_row(r, overrides) for r in sorted(ROLE_FIELDS)]
    return {"roles": roles, "switchable": sorted(SWITCHABLE_ROLE_FIELDS)}


@router.get("/models/available")
async def get_available_models() -> dict:
    """§17.817 (plan 5.7) — the pulled Ollama tag list for the wizard's
    pickers, split local vs cloud. ``reachable: false`` (with an empty list)
    when the daemon is down — the wizard renders that state instead of a
    spinner-forever."""
    tags = await _pulled_tags()
    if tags is None:
        return {"reachable": False, "ollama_url": settings.ollama_base_url,
                "local": [], "cloud": []}
    names = sorted(t for t in tags if t)
    return {
        "reachable": True,
        "ollama_url": settings.ollama_base_url,
        "local": [t for t in names if not _is_cloud_tag(t)],
        "cloud": [t for t in names if _is_cloud_tag(t)],
    }


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
    tags = await _pulled_tags()
    if tags is not None and model not in tags:
        raise HTTPException(
            status_code=422, detail=f"model {model!r} is not a pulled Ollama tag",
        )
    # Cloud tags list even after retirement (stale-200) — probe on *generate*.
    if body.probe and _is_cloud_tag(model):
        probe = await _generate_probe(model)
        if not probe["ok"]:
            raise HTTPException(
                status_code=422,
                detail=f"model {model!r} failed the generate probe: {probe['error']}",
            )
    try:
        await set_override(role, model, db)
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
    that first call is also what a real request would pay (model load)."""
    model = (body.model or "").strip()
    if not model:
        raise HTTPException(status_code=422, detail="model tag is empty")
    result = await _generate_probe(model, timeout=120.0)
    return {"model": model, **result}
