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
    from mcp import ClientSession

    spec.validate()
    timeout = settings.mcp_call_timeout

    if spec.transport == "stdio":
        from mcp.client.stdio import (
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
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    # §17.1244 — the transport's read timeout, which nothing was setting.
    #
    # `streamable_http_client` in this SDK takes no timeout of its own: it uses
    # the httpx client it is given, or builds one with httpx's defaults. httpx2's
    # default is **5 seconds**. So every call over this transport had a 5-second
    # read timeout while `ClientSession` was told 60 — and a tool call whose
    # answer took longer than five seconds lost its response with
    #
    #     MCPError: SSE stream ended without a response
    #
    # Three supervised runs died that way in one evening, each on a command that
    # is simply not instant: `pct start 111` (ADD50), `pct start 120` (ADD110),
    # `pveam update && pveam available …` (ADD111). Everything that succeeded was
    # fast — `pct list`, `pct status`, `curl` against a local API. The engine
    # then reported "the connection to pve-runner dropped", which read as a flaky
    # helper and was actually our own five-second clock.
    #
    # One client for both branches, with the read timeout tied to the call
    # timeout: the answer arrives on the SSE stream, so that stream has to be
    # allowed to stay quiet for as long as the command may run.
    _timeout = httpx2.Timeout(connect=10.0, read=timeout + 30.0, write=30.0, pool=10.0)
    async with httpx2.AsyncClient(headers=spec.headers or None, timeout=_timeout) as http_client:
        async with streamable_http_client(spec.endpoint, http_client=http_client) as streams:
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
    except Exception as exc:
        raise McpError(
            f"mcp server {spec.name!r}: list_tools failed: {_describe_exc(exc)}"
        ) from exc

    _tool_cache[spec.name] = (now + settings.mcp_session_ttl, tools)
    return tools


#: §17.1205 — kept here as a literal rather than imported, so recording cannot
#: make a tool call fail on an import error. `runner_activity.COMMAND_TOOLS` is
#: the same tuple and a test holds them equal.
_COMMAND_TOOLS = ("run_readonly", "run_supervised")


def _note_failure(act, spec, tool_name: str, args: dict, exc: BaseException) -> None:
    if act is None:
        return
    try:
        act[0].note_failure(spec, tool_name, args, exc, act[1])
    except Exception as err:                                # pragma: no cover
        logger.warning("runner_activity_note_failed err=%r", err)


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

    # §17.1205 — the ONE place every runner command is sent, so the one place it
    # is recorded for the operator to watch. Not at the callers (`run_probes`,
    # `run_block`, the walkthrough look-ups, the guest checks): four sites that
    # would drift. Only the tools that carry a command — the tool listings and
    # `write_policy` are the engine asking a runner about itself, and the probe
    # calls those on every page load.
    _act = None
    if tool_name in _COMMAND_TOOLS:
        try:
            from app.modules import runner_activity as _ra
            _act = (_ra, _ra.started(spec.name))
        except Exception:                                   # never break the call
            _act = None

    try:
        out = await asyncio.wait_for(_do(), timeout=settings.mcp_call_timeout + 10.0)
    except asyncio.TimeoutError as exc:
        _note_failure(_act, spec, tool_name, args, exc)
        raise McpError(
            f"mcp server {spec.name!r} tool {tool_name!r}: timed out"
        ) from exc
    except McpError as exc:
        _note_failure(_act, spec, tool_name, args, exc)
        raise
    except Exception as exc:
        _note_failure(_act, spec, tool_name, args, exc)
        raise McpError(
            f"mcp server {spec.name!r} tool {tool_name!r}: {_describe_exc(exc)}"
        ) from exc
    if _act is not None:
        try:
            _act[0].note_result(spec, tool_name, args, out, _act[1])
        except Exception as exc:
            logger.warning("runner_activity_note_failed err=%r", exc)

    if out.is_error:
        raise McpToolError(
            f"mcp server {spec.name!r} tool {tool_name!r} returned an error: {out.text[:500]}"
        )
    return out
