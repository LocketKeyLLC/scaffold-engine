"""§17.1193 — connecting a machine is a SETTING, not a planning job.

Until now the only way to point the engine at a machine was to press "Walk me
through it" on a capability card, which opened a research + plan + walkthrough
job — nine open questions, a feasibility score and a DAG — to record a host, a
port and a token. The operator said it plainly: *"why am I making a whole
'nother plan just to connect it??? shouldn't the option already be programmed
in?"* They are right, and every comparable tool agrees: Ansible has an
inventory, AWX and Jenkins have a Credentials form, Portainer has "Add
environment", Proxmox itself has "Add node". None of them plan first.

So: a form's worth of endpoints. The walkthrough stays for anyone who wants to
be walked through it — it is no longer the only door.

Everything here is admin-only and the values it stores are write-only from the
client's side: a secret goes in and never comes back out.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz import require_admin
from app.database import get_db

logger = logging.getLogger("scaffold")

router = APIRouter(tags=["Setup"], dependencies=[Depends(require_admin)])

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,253}$")


class ConnectInput(BaseModel):
    """What it takes to reach a machine. Nothing else is required of anyone."""
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=8790, ge=1, le=65535)
    name: str = Field(default="pve-runner", min_length=1, max_length=64)
    token: str | None = Field(default=None, max_length=256)
    label: str | None = Field(default=None, max_length=200)


class SecretInput(BaseModel):
    value: str = Field(min_length=1, max_length=4096)
    hint: str = Field(default="", max_length=200)
    runner: str | None = Field(default=None, max_length=64)


@router.get("/setup/machines")
async def list_machines(db: AsyncSession = Depends(get_db)) -> dict:
    """Everything the connection page shows, in one call: the registered
    runner, what the engine last learned about it, the values it holds by name,
    and the command prefixes this operator's plan needs.

    Read-only and cheap — it does NOT call out to the machine. `POST
    /setup/machines/probe` is the button that does, because that costs seconds.
    """
    from app.config import settings
    from app.modules import assist_local_runner as _lr
    from app.modules import assist_supervised as _sw
    from app.modules import engine_setup as _es
    from app.modules import runner_secrets as _rs

    spec = None
    try:
        spec = await _lr.runner_spec(db)
    except Exception as exc:
        logger.warning("machines_lookup_failed err=%r", exc)
    known_policy, policy = (_sw.cached_policy(spec) if spec is not None else (False, None))
    ctx = await _es.recipe_context(db)
    prefixes = await _es.prefixes_needed(db)
    reads = await _es.read_prefixes_needed(db)      # §17.1198 — the READ grant
    # §17.1198 — the installer REPLACES the runner's lists, so a recommendation
    # that omits what is already allowed silently takes it away. Live, the write
    # list had moved on to the next step's needs and `qm start` — granted ten
    # minutes earlier, and the reason the current step can run at all — had
    # dropped off it. Anything already allowed stays, said plainly.
    already = list((policy or {}).get("allow") or [])
    have = {p["prefix"] for p in prefixes}
    prefixes += [{"prefix": a, "steps": [], "why": "already allowed on this runner"}
                 for a in already if a not in have]
    # §17.1193 — the value store is one PART of this page. A schema that has not
    # caught up (the code deployed, `alembic upgrade head` still to run) must
    # degrade that one card, not 500 the whole connection screen — which is
    # exactly the screen an operator opens when something is wrong. Caught by
    # running the endpoint against a database without the table.
    secrets: list[dict] = []
    secrets_error = ""
    try:
        secrets = await _rs.list_secrets(db)
    except Exception as exc:
        await db.rollback()
        secrets_error = "the value store is not available yet (the database migration has not run)"
        logger.warning("machines_secrets_unavailable err=%r", exc)
    endpoint = str(getattr(spec, "endpoint", "") or "")
    m = re.match(r"https?://([^:/]+):?(\d+)?", endpoint)
    return {
        "connected": spec is not None,
        "runner": {
            "name": getattr(spec, "name", None),
            "host": m.group(1) if m else None,
            "port": int(m.group(2)) if (m and m.group(2)) else _es.RUNNER_PORT,
            "endpoint": endpoint or None,
            "enabled": bool(getattr(spec, "enabled", False)),
            "description": getattr(spec, "description", None),
        } if spec is not None else None,
        "write_channel": {
            "checked": known_policy,
            "open": bool(policy),
            "allow": list((policy or {}).get("allow") or []),
            "sudo": bool((policy or {}).get("sudo")),
            "helper": (policy or {}).get("helper"),
            # §17.1194 — `sudo: false` is not a detail. The helper runs as an
            # unprivileged user, so on a Proxmox host every allow-listed
            # command (`qm`, `pct`, `pvesm`) comes back "permission denied" —
            # after the operator approved it. Live cause: the host has no
            # `sudo` package at all, so the installer's visudo step could not
            # run and the grant was never written.
            "privilege_note": ("" if not policy or policy.get("sudo") else
                               "These run as the runner's unprivileged user, so anything needing root — qm, pct and "
                               "pvesm on a Proxmox host — will be refused by the machine itself. The install could not "
                               "write the root grant: that step needs the `sudo` package on the target (`apt install "
                               "sudo`), after which re-running the line above grants root for exactly these prefixes "
                               "and nothing else."),
        },
        "secrets": secrets,
        "secrets_error": secrets_error,
        "needed_prefixes": prefixes,
        "needed_read_prefixes": reads,
        "install": _es.install_line(ctx, prefixes=[p["prefix"] for p in prefixes if p["prefix"]],
                                    sudo_allow=[p["prefix"] for p in reads if p["prefix"]]),
        "mcp_enabled": bool(settings.mcp_tool_enabled),
    }


@router.post("/setup/machines/connect")
async def connect_machine(body: ConnectInput, db: AsyncSession = Depends(get_db)) -> dict:
    """Point the engine at a machine and say at once whether it answered.

    Registers (or re-points) the runner row and probes it in the same call, so
    the form reports a real result instead of "saved" — the failure mode this
    replaces is a walkthrough that records an address and finds out ten minutes
    later that nothing is there.
    """
    from app.modules import engine_setup as _es
    from app.modules.mcp_registry import McpServerSpec, upsert_server

    host = body.host.strip()
    if not _HOST_RE.match(host):
        raise HTTPException(status_code=422, detail="host must be a hostname or an address")
    token = (body.token or "").strip() or await _es._registered_runner_token(db)
    if not token:
        import secrets as _secrets
        token = _secrets.token_hex(24)
    spec = McpServerSpec(
        name=body.name.strip(), transport="streamable_http",
        endpoint=f"http://{host}:{body.port}/mcp/",
        headers={"X-Runner-Token": token}, enabled=True,
        description=f"{_es.LOCAL_RUNNER_MARK} {body.label or ('connected from Settings → Machines')}",
    )
    try:
        spec.validate()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await upsert_server(db, spec)
    await db.commit()
    try:
        from app.modules import mcp_client
        mcp_client.clear_tool_cache(spec.name)
        from app.modules import assist_supervised as _sw
        _sw.clear_policy_cache(spec.name)
    except Exception as exc:
        logger.warning("machines_cache_clear_failed err=%r", exc)
    logger.warning("machine_connected name=%s endpoint=%s", spec.name, spec.endpoint)
    probe = await _es.probe_local_runner(db)
    return {"name": spec.name, "endpoint": spec.endpoint, "token": token, "probe": probe}


@router.post("/setup/machines/probe")
async def probe_machine(db: AsyncSession = Depends(get_db)) -> dict:
    """Ask the machine, now. Seconds, and it is the operator's button."""
    from app.modules import engine_setup as _es
    return await _es.probe_local_runner(db)


@router.get("/setup/secrets")
async def list_secrets_endpoint(db: AsyncSession = Depends(get_db)) -> dict:
    """Names and metadata. A value never comes back out of this API."""
    from app.modules import runner_secrets as _rs
    return {"secrets": await _rs.list_secrets(db)}


@router.put("/setup/secrets/{name}")
async def put_secret(name: str, body: SecretInput, db: AsyncSession = Depends(get_db)) -> dict:
    """Store one value, encrypted at rest, referenced from then on by name."""
    from app.modules import runner_secrets as _rs
    try:
        return await _rs.set_secret(db, name, body.value, runner=body.runner, hint=body.hint)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/setup/secrets/{name}")
async def delete_secret_endpoint(name: str, db: AsyncSession = Depends(get_db)) -> dict:
    from app.modules import runner_secrets as _rs
    return {"name": name, "deleted": await _rs.delete_secret(db, name)}
