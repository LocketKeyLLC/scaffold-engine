"""MCP client — talk to an external MCP server described by an ``McpServerSpec``
(§17.772). Backs the ``tool='MCP'`` DAG node type and the router's tool-list
introspection endpoint.

Design notes
------------
* **Connect-per-call.** Each ``list_tools``/``call_tool`` opens a fresh
  transport + session and tears it down. MCP sessions are built on anyio task
  groups whose cancel scopes are bound to the spawning task, so caching a live
  session across unrelated asyncio tasks is a correctness hazard, not an
  optimization. The connect handshake is cheap next to an LLM node, so we pay it.
* **Tool-list cache.** The *result* of ``list_tools`` (plain data) is cached per
  server for ``settings.mcp_session_ttl`` seconds — this is what makes repeated
  introspection (and DAG-generator tool discovery) cheap without holding a live
  connection.
* **Async-first.** The SDK is fully async (anyio + httpx2); no blocking calls,
  so no ``run_in_executor`` wrapping is needed (unlike PyMilvus/CrossEncoder).
* **Both transports.** ``streamable_http`` (URL, optional auth headers via a
  custom httpx2 client) and ``stdio`` (subprocess).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.config import settings
from app.modules.mcp_registry import McpServerSpec

logger = logging.getLogger("scaffold.mcp")


def _describe_exc(exc: BaseException) -> str:
    """Flatten an exception into a readable cause. anyio task groups (used by
    the stdio/http transports) raise ``ExceptionGroup``, whose default str is
    the useless 'unhandled errors in a TaskGroup (N sub-exception)'. Unwrap to
    the leaf causes so a misconfigured server yields an actionable message."""
    subs = getattr(exc, "exceptions", None)
    if subs:
        return "; ".join(_describe_exc(e) for e in subs)
    return f"{type(exc).__name__}: {exc}"


class McpError(RuntimeError):
    """Transport/protocol failure reaching or handshaking with a server."""


class McpToolError(McpError):
    """The tool ran but the server reported an error result (isError=True)."""


@dataclass
class McpToolResult:
    text: str
    is_error: bool = False
    structured: Any | None = None
    raw_content: list[Any] = field(default_factory=list)


# ---- tool-list cache (plain data, TTL'd) -----------------------------------
# name -> (expires_at_monotonic, list[dict])
_tool_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def clear_tool_cache(name: str | None = None) -> None:
    if name is None:
        _tool_cache.clear()
    else:
        _tool_cache.pop(name, None)


# ---- session plumbing ------------------------------------------------------
@asynccontextmanager
async def _open_session(spec: McpServerSpec) -> AsyncIterator[Any]:
    """Yield an initialized ``ClientSession`` for ``spec``. Imports of the MCP
    SDK are function-local so a repo without the dep (or a disabled feature)
    never pays the import at module load."""
    from mcp import ClientSession  # noqa: PLC0415

    spec.validate()
    timeout = settings.mcp_call_timeout

    if spec.transport == "stdio":
        from mcp.client.stdio import (  # noqa: PLC0415
            StdioServerParameters,
            get_default_environment,
            stdio_client,
        )

        env = None
        if spec.env:
            # Merge over the inherited default env rather than replacing it,
            # so the child still sees PATH/HOME/etc.
            env = {**get_default_environment(), **spec.env}
        params = StdioServerParameters(
            command=spec.command, args=list(spec.args or []), env=env
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                await session.initialize()
                yield session
        return

    # streamable_http
    from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415

    if spec.headers:
        import httpx2  # noqa: PLC0415 — mcp's own httpx (2.x), separate from our httpx 0.28

        async with httpx2.AsyncClient(headers=spec.headers) as http_client:
            async with streamable_http_client(spec.endpoint, http_client=http_client) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                    await session.initialize()
                    yield session
    else:
        async with streamable_http_client(spec.endpoint) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                await session.initialize()
                yield session


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    return {
        "name": getattr(tool, "name", None),
        "description": getattr(tool, "description", None) or "",
        "input_schema": getattr(tool, "inputSchema", None)
        or getattr(tool, "input_schema", None)
        or {},
    }


def _is_error(result: Any) -> bool:
    """mcp 2.0 exposes snake_case ``is_error``; older builds used camelCase
    ``isError``. Accept either so a minor SDK bump can't silently mask a tool
    failure as success."""
    flag = getattr(result, "is_error", None)
    if flag is None:
        flag = getattr(result, "isError", False)
    return bool(flag)


def _result_to_text(result: Any) -> str:
    """Flatten a CallToolResult into a single string suitable for a node's
    output_text. Prefers structured content when present."""
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if structured:
        try:
            return json.dumps(structured, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    parts: list[str] = []
    for block in (getattr(result, "content", None) or []):
        txt = getattr(block, "text", None)
        if txt is not None:
            parts.append(txt)
        else:
            data = getattr(block, "data", None)
            parts.append(f"[{getattr(block, 'type', 'content')}]" if data else str(block))
    return "\n".join(parts).strip()


async def list_tools(spec: McpServerSpec, *, use_cache: bool = True) -> list[dict[str, Any]]:
    """Return ``[{name, description, input_schema}, ...]`` for a server."""
    now = time.monotonic()
    if use_cache:
        hit = _tool_cache.get(spec.name)
        if hit and hit[0] > now:
            return hit[1]

    async def _do() -> list[dict[str, Any]]:
        async with _open_session(spec) as session:
            resp = await session.list_tools()
            return [_tool_to_dict(t) for t in (resp.tools or [])]

    try:
        tools = await asyncio.wait_for(_do(), timeout=settings.mcp_call_timeout + 10.0)
    except asyncio.TimeoutError as exc:
        raise McpError(f"mcp server {spec.name!r}: list_tools timed out") from exc
    except Exception as exc:  # noqa: BLE001 — normalize transport errors
        raise McpError(
            f"mcp server {spec.name!r}: list_tools failed: {_describe_exc(exc)}"
        ) from exc

    _tool_cache[spec.name] = (now + settings.mcp_session_ttl, tools)
    return tools


async def call_tool(
    spec: McpServerSpec, tool_name: str, arguments: dict[str, Any] | None = None
) -> McpToolResult:
    """Invoke one tool on a server and return its flattened result.

    Raises ``McpError`` on transport failure and ``McpToolError`` when the
    server returns an error result (``isError=True``)."""
    args = arguments or {}

    async def _do() -> McpToolResult:
        async with _open_session(spec) as session:
            result = await session.call_tool(tool_name, args)
            return McpToolResult(
                text=_result_to_text(result),
                is_error=_is_error(result),
                structured=getattr(result, "structured_content", None)
                or getattr(result, "structuredContent", None),
                raw_content=list(getattr(result, "content", None) or []),
            )

    try:
        out = await asyncio.wait_for(_do(), timeout=settings.mcp_call_timeout + 10.0)
    except asyncio.TimeoutError as exc:
        raise McpError(
            f"mcp server {spec.name!r} tool {tool_name!r}: timed out"
        ) from exc
    except McpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise McpError(
            f"mcp server {spec.name!r} tool {tool_name!r}: {_describe_exc(exc)}"
        ) from exc

    if out.is_error:
        raise McpToolError(
            f"mcp server {spec.name!r} tool {tool_name!r} returned an error: {out.text[:500]}"
        )
    return out
