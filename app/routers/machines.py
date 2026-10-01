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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz import require_admin
from app.database import get_db

logger = logging.getLogger("scaffold")


#: The host and port an endpoint names. §17.1204 — the install line and the
#: runner block both read the port, and they must agree on it.
def endpoint_parts(endpoint) -> tuple[str | None, int | None]:
    m = re.match(r"https?://([^:/]+):?(\d+)?", str(endpoint or ""))
    if not m:
        return None, None
    return m.group(1), (int(m.group(2)) if m.group(2) else None)


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
        # §17.1205 — INCLUDING a paused one. `runner_spec` hides a disabled row
        # from every consumer, which is the point of pausing — but this page is
        # where you un-pause it, so if it vanished here the pause would be a
        # one-way door.
        spec = await _lr.runner_spec(db, include_disabled=True)
    except Exception as exc:
        logger.warning("machines_lookup_failed err=%r", exc)
    paused = spec is not None and not spec.enabled
    known_policy, policy = (_sw.cached_policy(spec) if spec is not None else (False, None))
    # §17.1204 — the write-channel view above cannot answer what else `--install`
    # would replace (the root-read list, the values file). Cache-only, because
    # this endpoint is documented as not calling out.
    _, setup = (_sw.cached_setup(spec) if spec is not None else (False, None))
    ctx = await _es.recipe_context(db)
    # §17.1204 — build the line for the port the runner is actually registered
    # on, not the default, so it agrees with the endpoint shown beside it.
    _host, _port = endpoint_parts(getattr(spec, "endpoint", "")) if spec is not None else (None, None)
    if _port:
        ctx = {**ctx, "port": _port}
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
    # §17.1204 — the READ list is replaced by the same `--install`, so it needed
    # the same union. It did not have one: a runner granted `pvesm status` by
    # hand lost it the moment the plan stopped mentioning it.
    #
    # `ANY` is skipped, and that is not a detail: a trusted runner REPORTS
    # `sudo_allow: ["ANY"]` because §17.1202 derives it from the write grant, so
    # unioning it verbatim would put `--sudo-allow "ANY"` on the ENUMERATED
    # line — the one whose whole purpose is to be the alternative to trusting
    # the machine — and show "ANY" as a read prefix the plan asked for.
    read_have = {p["prefix"] for p in reads} | {_sw.ANY}
    reads += [{"prefix": a, "why": "already allowed on this runner"}
              for a in list((setup or {}).get("sudo_allow") or []) if a and a not in read_have]
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
    # §17.1204 — `--install` REPLACES the service, so a line without
    # `--secrets-file` unwires the file the runner resolves `$NAME` from and
    # every value-bearing command starts failing. Prefer the path the runner
    # reports (v17); the recipe's path when it holds values but did not say.
    _secrets_file = ((setup or {}).get("secrets_file")
                     or (_es.RUNNER_SECRETS_PATH if (secrets or (setup or {}).get("secrets")) else None))
    endpoint = str(getattr(spec, "endpoint", "") or "")
    return {
        "connected": spec is not None and not paused,
        "paused": paused,
        "runner": {
            "name": getattr(spec, "name", None),
            "host": _host,
            "port": _port or _es.RUNNER_PORT,
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
                                    sudo_allow=[p["prefix"] for p in reads if p["prefix"]],
                                    secrets_file=_secrets_file),
        # §17.1199 — the other way to set this machine up. The enumerated list
        # can only be completed by failing, one console round-trip per command
        # nobody predicted; this trusts the machine with whatever the operator
        # approves, which is the decision they are already making per block.
        "install_trusted": _es.install_line(ctx, prefixes=[_sw.ANY], secrets_file=_secrets_file),
        # §17.1272 — "list" was asserted whenever the policy was MISSING, which
        # on a cold cache is every page load after a restart. The page then told
        # an operator whose runner is set to ANY that it was in list mode, and
        # offered the "turn trust on" line for trust they already had. Unknown
        # is its own answer and the only honest one here.
        "trust_mode": ("unknown" if not known_policy else
                       ("approve" if _sw.ANY in ((policy or {}).get("allow") or []) else "list")),
        # §17.1272 — and the install lines are only safe to offer once that is
        # known. They REPLACE the runner's lists, and §17.1198/§17.1204 keep them
        # from taking anything away by unioning in what the machine already
        # allows — read from `policy`. With no policy that union is empty, so the
        # very mechanism that prevents a downgrade silently produces one: the
        # line built on a cold cache dropped ANY, `pct create` and `lvremove`
        # from a trusted runner. A line nobody can vouch for is not offered.
        "install_ready": bool(known_policy),
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


class RunInput(BaseModel):
    """One read-only command to run on the connected machine, now."""
    command: str = Field(min_length=1, max_length=2000)


@router.post("/setup/machines/pause")
async def pause_machine(paused: bool = True, db: AsyncSession = Depends(get_db)) -> dict:
    """§17.1205 — stop (or resume) the engine USING this machine.

    Not `systemctl stop` on the target: the runner refuses commands touching its
    own service by design, and the engine has no other channel to it. This flips
    the registry row's `enabled` flag, which is what every caller checks
    (`runner_spec` returns None for a disabled row), so the helper keeps running
    there and the engine simply stops sending it anything. Reversible, instant,
    and it does not discard the token the way Forget does.
    """
    from app.modules import assist_local_runner as _lr
    from app.modules import assist_supervised as _sw
    from app.modules import mcp_client, mcp_registry
    spec = await _lr.runner_spec(db, include_disabled=True)
    if spec is None:
        raise HTTPException(status_code=404, detail="no machine is connected")
    spec.enabled = not paused
    await mcp_registry.upsert_server(db, spec)
    await db.commit()
    mcp_client.clear_tool_cache(spec.name)
    _sw.clear_policy_cache(spec.name)
    logger.warning("machine_%s name=%s", "paused" if paused else "resumed", spec.name)
    return {"name": spec.name, "enabled": spec.enabled, "paused": paused}


@router.post("/setup/machines/run")
async def run_on_machine(body: RunInput, db: AsyncSession = Depends(get_db)) -> dict:
    """§17.1205 — run ONE read-only command on the connected machine and return
    what it printed. The operator could see the runner existed and not ask it
    anything.

    Gated by the same deterministic `read_only_command` the state check uses, so
    this is not a shell: a command that could write is refused here, before it is
    sent, and the runner refuses it again on its own side.
    """
    from app.modules import assist_local_runner as _lr
    from app.modules.assist_state_check import read_only_command
    from app.modules.mcp_client import call_tool
    cmd = body.command.strip()
    if not read_only_command(cmd):
        raise HTTPException(status_code=422, detail={
            "error": "that command is not read-only, so it will not be sent",
            "hint": "this box only reads. A command that changes the machine goes through a step you approve."})
    spec = await _lr.runner_spec(db)
    if spec is None:
        raise HTTPException(status_code=409, detail="no machine is connected (or it is paused)")
    try:
        res = await call_tool(spec, "run_readonly", {"command": cmd, "timeout_s": 20})
        out = _lr._plain_output(res)
    except Exception as exc:
        logger.warning("machine_run_failed cmd=%r err=%r", cmd[:80], exc)
        return {"command": cmd, "ran": False, "why": "the call to the runner did not come back",
                "output": f"(runner error: {exc})"}
    why = _lr.not_evidence(out, bool(res.is_error))
    return {"command": cmd, "ran": not why, "why": why, "output": out}


@router.get("/setup/machines/activity")
async def machine_activity(limit: int = Query(40, ge=1, le=200),
                           db: AsyncSession = Depends(get_db)) -> dict:
    """§17.1205 — what this machine has been asked to do, newest first.

    In memory and bounded, so `since` says when the count starts from: the page
    must not imply a durable log it does not have.
    """
    from app.modules import assist_local_runner as _lr
    from app.modules import runner_activity as _ra
    name = None
    try:
        spec = await _lr.runner_spec(db)
        name = getattr(spec, "name", None)
    except Exception as exc:
        logger.warning("machine_activity_lookup_failed err=%r", exc)
    return {"runner": name, "entries": _ra.recent(runner=name, limit=limit),
            **_ra.summary(runner=name)}


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
