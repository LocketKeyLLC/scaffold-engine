"""MCP server registry + introspection endpoints (§17.772).

Manages the ``mcp_servers`` DB registry (the runtime-editable override over the
``settings.mcp_servers_config`` seed) and provides live tool discovery / a
debug call surface for servers the engine consumes as DAG nodes (tool='MCP').

Registry CRUD is always available (it only writes DB rows). The two endpoints
that actually *connect out* — ``/tools`` and ``/call`` — are gated on
``settings.mcp_tool_enabled`` so the whole outbound surface sits behind one
flag, matching the tool-executor gate in ``execute_next_node``.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.modules import mcp_client, mcp_registry
from app.modules.mcp_registry import McpServerSpec

router = APIRouter(prefix="/mcp", tags=["MCP"])


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
