"""MCP server registry — the set of external MCP servers the engine may call
as DAG nodes (tool='MCP'), §17.772.

Two sources, merged by name:

  1. **Config seed** — ``settings.mcp_servers_config`` (a JSON array). Read-only,
     survives nothing but a redeploy; good for baking in a fixed fleet.
  2. **DB table** — ``mcp_servers`` (migration 060). Runtime-managed via
     ``app/routers/mcp.py``; a DB row **overrides** a config entry with the
     same ``name``.

The merge policy (DB-over-config) mirrors the valve/env fallback pattern used
elsewhere: a durable, operator-set value wins over the shipped default.

This module owns only *specs and persistence*. Actually talking to a server
(list/call tools) lives in ``app/modules/mcp_client.py``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger("scaffold.mcp")

Transport = Literal["streamable_http", "stdio"]

_SELECT_COLS = (
    "name, transport, endpoint, command, args, env, headers, enabled, description"
)


@dataclass
class McpServerSpec:
    """A single consumable MCP server, normalized across both sources."""

    name: str
    transport: Transport
    endpoint: str | None = None            # streamable_http URL
    command: str | None = None             # stdio launcher
    args: list[str] = field(default_factory=list)   # stdio argv tail
    env: dict[str, str] | None = None      # stdio subprocess env overrides
    headers: dict[str, str] | None = None  # streamable_http request headers
    enabled: bool = True
    description: str | None = None
    source: Literal["config", "db"] = "db"

    def validate(self) -> None:
        if self.transport not in ("streamable_http", "stdio"):
            raise ValueError(
                f"mcp server {self.name!r}: unknown transport {self.transport!r}"
            )
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"mcp server {self.name!r}: stdio requires 'command'")
        if self.transport == "streamable_http" and not self.endpoint:
            raise ValueError(
                f"mcp server {self.name!r}: streamable_http requires 'endpoint'"
            )

    def public_dict(self) -> dict[str, Any]:
        """Serialize for API responses. Redacts header/env *values* — they may
        carry bearer tokens — while keeping the key names visible."""
        return {
            "name": self.name,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "command": self.command,
            "args": list(self.args or []),
            "env_keys": sorted((self.env or {}).keys()),
            "header_keys": sorted((self.headers or {}).keys()),
            "enabled": self.enabled,
            "description": self.description,
            "source": self.source,
        }


def _coerce_json(value: Any, default: Any) -> Any:
    """asyncpg may hand back a JSONB column as a str or as decoded Python.
    Normalize either into a Python object."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def _spec_from_dict(entry: dict[str, Any], *, source: Literal["config", "db"]) -> McpServerSpec:
    if not isinstance(entry, dict):
        raise ValueError(f"mcp server entry must be an object, got {type(entry).__name__}")
    name = entry.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("mcp server entry missing a string 'name'")
    return McpServerSpec(
        name=name,
        transport=entry.get("transport"),  # validated below
        endpoint=entry.get("endpoint"),
        command=entry.get("command"),
        args=list(_coerce_json(entry.get("args"), []) or []),
        env=_coerce_json(entry.get("env"), None),
        headers=_coerce_json(entry.get("headers"), None),
        enabled=bool(entry.get("enabled", True)),
        description=entry.get("description"),
        source=source,
    )


def parse_config_seed() -> dict[str, McpServerSpec]:
    """Parse ``settings.mcp_servers_config`` into name→spec. Never raises —
    a malformed seed logs and yields the valid subset (a bad env var must not
    brick the orchestrator at import)."""
    raw = (settings.mcp_servers_config or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("mcp_servers_config is not valid JSON: %s", exc)
        return {}
    if not isinstance(data, list):
        logger.error("mcp_servers_config must be a JSON array, got %s", type(data).__name__)
        return {}
    out: dict[str, McpServerSpec] = {}
    for entry in data:
        try:
            spec = _spec_from_dict(entry, source="config")
            spec.validate()
            out[spec.name] = spec
        except Exception as exc:  # noqa: BLE001 — skip-and-log a bad entry
            logger.error("skipping invalid mcp_servers_config entry: %s", exc)
    return out


async def _db_servers(db: AsyncSession) -> dict[str, McpServerSpec]:
    rows = (await db.execute(text(f"SELECT {_SELECT_COLS} FROM mcp_servers"))).mappings().all()
    out: dict[str, McpServerSpec] = {}
    for r in rows:
        out[r["name"]] = McpServerSpec(
            name=r["name"],
            transport=r["transport"],
            endpoint=r["endpoint"],
            command=r["command"],
            args=list(_coerce_json(r["args"], []) or []),
            env=_coerce_json(r["env"], None),
            headers=_coerce_json(r["headers"], None),
            enabled=bool(r["enabled"]),
            description=r["description"],
            source="db",
        )
    return out


async def list_servers(db: AsyncSession, *, include_disabled: bool = False) -> list[McpServerSpec]:
    """Merged view (config seed under DB override), sorted by name."""
    merged: dict[str, McpServerSpec] = dict(parse_config_seed())
    merged.update(await _db_servers(db))  # DB wins by name
    specs = list(merged.values())
    if not include_disabled:
        specs = [s for s in specs if s.enabled]
    return sorted(specs, key=lambda s: s.name)


async def get_server(db: AsyncSession, name: str) -> McpServerSpec | None:
    """Resolve a single server by name (DB overrides config). Returns even
    disabled servers — the caller decides whether 'disabled' is an error."""
    db_rows = await _db_servers(db)
    if name in db_rows:
        return db_rows[name]
    return parse_config_seed().get(name)


async def upsert_server(db: AsyncSession, spec: McpServerSpec) -> McpServerSpec:
    """Create or update a DB-backed server. Config-seed servers cannot be
    edited here — a DB row with the same name simply overrides them."""
    spec.source = "db"
    spec.validate()
    await db.execute(
        text(
            """
            INSERT INTO mcp_servers
                (name, transport, endpoint, command, args, env, headers, enabled, description)
            VALUES
                (:name, :transport, :endpoint, :command,
                 CAST(:args AS jsonb), CAST(:env AS jsonb), CAST(:headers AS jsonb),
                 :enabled, :description)
            ON CONFLICT (name) DO UPDATE SET
                transport   = EXCLUDED.transport,
                endpoint    = EXCLUDED.endpoint,
                command     = EXCLUDED.command,
                args        = EXCLUDED.args,
                env         = EXCLUDED.env,
                headers     = EXCLUDED.headers,
                enabled     = EXCLUDED.enabled,
                description = EXCLUDED.description
            """
        ),
        {
            "name": spec.name,
            "transport": spec.transport,
            "endpoint": spec.endpoint,
            "command": spec.command,
            "args": json.dumps(list(spec.args or [])),
            "env": json.dumps(spec.env) if spec.env is not None else None,
            "headers": json.dumps(spec.headers) if spec.headers is not None else None,
            "enabled": spec.enabled,
            "description": spec.description,
        },
    )
    return spec


async def delete_server(db: AsyncSession, name: str) -> bool:
    """Delete a DB-backed server. Returns False if no DB row existed (a
    config-seed entry of the same name is untouched and re-emerges)."""
    result = await db.execute(
        text("DELETE FROM mcp_servers WHERE name = :name"), {"name": name}
    )
    return bool(result.rowcount)
