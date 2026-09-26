"""MCP server registry + introspection endpoints (§17.772).

Manages the ``mcp_servers`` DB registry (the runtime-editable override over the
``settings.mcp_servers_config`` seed) and provides live tool discovery / a
debug call surface for servers the engine consumes as DAG nodes (tool='MCP').

§17.1172 — the WHOLE router is admin-only. It used to carry no dependency at
all, with the docstring reasoning "Registry CRUD is always available (it only
writes DB rows)". That premise was false: a row with ``transport="stdio"`` is a
command line, and ``mcp_client._open_session`` turns it into
``StdioServerParameters(command=…, args=…, env=…)`` → ``stdio_client(params)``
— a subprocess INSIDE the orchestrator container, which holds DATABASE_URL,
SCAFFOLD_API_KEY, GITHUB_TOKEN and the Fernet secret. Under
``multi_user_enabled`` any issued key, role ``user`` included, could register
one; entering a session (``GET /servers/{name}/tools``) spawned it. Audit
2026-09-25.

The two endpoints that actually *connect out* — ``/tools`` and ``/call`` — stay
additionally gated on ``settings.mcp_tool_enabled`` so the whole outbound
surface sits behind one flag, matching the tool-executor gate in
``execute_next_node``. ``/call`` additionally re-applies the read-only gate
(§17.1172) — it is the one door to a registered runner that did not have one.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz import require_admin
from app.config import settings
from app.database import get_db
from app.modules import mcp_client, mcp_registry
from app.modules.mcp_registry import McpServerSpec

# §17.1172 — admin-only, router-wide. Registry writes spawn processes and the
# call surface reaches the operator's own machines; neither is a user-role act.
router = APIRouter(prefix="/mcp", tags=["MCP"], dependencies=[Depends(require_admin)])


class McpServerInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    transport: Literal["streamable_http", "stdio"]
    endpoint: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    enabled: bool = True
    description: str | None = None


class McpToolCallInput(BaseModel):
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


def _require_consumer_enabled() -> None:
    if not settings.mcp_tool_enabled:
        raise HTTPException(
            status_code=403,
            detail="MCP consumer disabled (set mcp_tool_enabled=true to connect out)",
        )


#: §17.1172 — tools whose argument IS a shell command. Invoking one of these
#: through this debug surface bypassed every gate the assist paths apply:
#: `assist_state_check.read_only_command` lives on the assist side, and this
#: handler called `mcp_client.call_tool` straight through. The helper's own gate
#: was the only check left, and the audit measured it accepting
#: `bash -lc '<anything>'`. Re-gate here so the engine never SENDS what it would
#: not send from a walkthrough.
_SHELL_TOOL_ARGS = {"run_readonly": "command"}


def _require_read_only(body: "McpToolCallInput") -> None:
    arg = _SHELL_TOOL_ARGS.get(body.tool)
    if arg is None:
        return
    from app.modules.assist_state_check import read_only_command

    cmd = str(body.arguments.get(arg) or "")
    if not read_only_command(cmd):
        raise HTTPException(
            status_code=422,
            detail=(
                f"refused: {body.tool!r} only runs read-only commands, and "
                f"{cmd[:120]!r} did not pass the engine's read-only gate"
            ),
        )


async def _resolve_enabled(db: AsyncSession, name: str) -> McpServerSpec:
    spec = await mcp_registry.get_server(db, name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"no MCP server named {name!r}")
    if not spec.enabled:
        raise HTTPException(status_code=409, detail=f"MCP server {name!r} is disabled")
    return spec


@router.get("/servers")
async def list_mcp_servers(
    include_disabled: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    specs = await mcp_registry.list_servers(db, include_disabled=include_disabled)
    return {"servers": [s.public_dict() for s in specs]}


@router.get("/servers/{name}")
async def get_mcp_server(name: str, db: AsyncSession = Depends(get_db)):
    spec = await mcp_registry.get_server(db, name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"no MCP server named {name!r}")
    return spec.public_dict()


@router.post("/servers")
async def upsert_mcp_server(body: McpServerInput, db: AsyncSession = Depends(get_db)):
    """Create or update a DB-backed server (overrides a config-seed entry of
    the same name)."""
    spec = McpServerSpec(**body.model_dump(), source="db")
    try:
        spec.validate()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await mcp_registry.upsert_server(db, spec)
    await db.commit()
    mcp_client.clear_tool_cache(spec.name)  # tools may have changed
    return spec.public_dict()


@router.delete("/servers/{name}")
async def delete_mcp_server(name: str, db: AsyncSession = Depends(get_db)):
    """Delete a DB-backed server. A config-seed entry of the same name is
    untouched and re-emerges in the merged view."""
    deleted = await mcp_registry.delete_server(db, name)
    await db.commit()
    mcp_client.clear_tool_cache(name)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"no DB-backed MCP server named {name!r}"
        )
    return {"deleted": name}


@router.get("/servers/{name}/tools")
async def list_mcp_server_tools(name: str, db: AsyncSession = Depends(get_db)):
    """Live tool discovery — connects to the server and lists its tools."""
    _require_consumer_enabled()
    spec = await _resolve_enabled(db, name)
    try:
        tools = await mcp_client.list_tools(spec)
    except mcp_client.McpError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"server": name, "tools": tools}


@router.post("/servers/{name}/call")
async def call_mcp_server_tool(
    name: str, body: McpToolCallInput, db: AsyncSession = Depends(get_db)
):
    """Debug/manual invocation of one tool on a registered server."""
    _require_consumer_enabled()
    spec = await _resolve_enabled(db, name)
    _require_read_only(body)
    try:
        result = await mcp_client.call_tool(spec, body.tool, body.arguments)
    except mcp_client.McpToolError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except mcp_client.McpError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "server": name,
        "tool": body.tool,
        "text": result.text,
        "is_error": result.is_error,
        "structured": result.structured,
    }
