"""Execute a DAG node tagged ``tool='MCP'`` (§17.772).

An MCP node is a *deterministic external call*, not an LLM generation — so it
short-circuits the inference/verify machinery in ``execute_next_node`` the same
way the human-review skip does. It reads its target and arguments from the
node's ``tool_config`` JSONB:

    { "server": "<registered name>", "tool": "<tool name>", "args": { ... } }

String values in ``args`` may reference upstream node outputs or the job brief
via ``${upstream.<node_key>}`` / ``${brief.<dotted.path>}`` placeholders, so a
plan can pipe one node's result into an MCP tool call.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules import mcp_client, mcp_registry

logger = logging.getLogger("scaffold.mcp")

_PLACEHOLDER = re.compile(r"\$\{\s*(upstream|brief)\.([A-Za-z0-9_.\-]+)\s*\}")


def parse_tool_config(raw: Any) -> dict[str, Any]:
    """Normalize a node's ``tool_config`` (JSONB may arrive as dict or str)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _upstream_text(value: Any) -> str:
    """Pull the output text out of an upstream_outputs value, which is a
    ``(text, confidence)`` tuple in the live path but may be a bare str/dict
    in tests."""
    if isinstance(value, (tuple, list)):
        return str(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("output_text") or value.get("text") or "")
    return "" if value is None else str(value)


def _lookup(scope: str, key: str, upstream: dict, brief: dict) -> str | None:
    if scope == "upstream":
        if key not in upstream:
            return None
        return _upstream_text(upstream[key])
    if scope == "brief":
        cur: Any = brief or {}
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        if cur is None:
            return None
        return cur if isinstance(cur, str) else json.dumps(cur)
    return None


def render_args(raw_args: Any, upstream: dict, brief: dict) -> Any:
    """Recursively substitute ``${upstream.KEY}`` / ``${brief.path}`` in string
    values. Unknown placeholders are left verbatim (fail-visible, not silent-
    empty). Non-string leaves pass through untouched."""
    if isinstance(raw_args, str):
        def _repl(m: re.Match) -> str:
            val = _lookup(m.group(1), m.group(2), upstream, brief)
            return val if val is not None else m.group(0)
        return _PLACEHOLDER.sub(_repl, raw_args)
    if isinstance(raw_args, dict):
        return {k: render_args(v, upstream, brief) for k, v in raw_args.items()}
    if isinstance(raw_args, list):
        return [render_args(v, upstream, brief) for v in raw_args]
    return raw_args


async def _fail(
    db: AsyncSession, node_id: Any, node_key: str, title: str | None, reason: str
) -> dict:
    """Mark the node failed with a verification reason (drives /exec/retry
    feedback) and return the failed summary dict."""
    await db.execute(
        text(
            "UPDATE dag_nodes SET status = 'failed', "
            "last_verification_reason = :r, completed_at = NOW() WHERE id = :nid"
        ),
        {"r": reason, "nid": str(node_id)},
    )
    await db.commit()
    logger.warning("mcp_node_failed: node=%s reason=%s", node_key, reason)
    return {
        "status": "failed",
        "node_key": node_key,
        "title": title,
        "error": reason,
        "verification_reason": reason,
        "reason": "mcp_error",
        "tool": "MCP",
        "message": reason,
    }


async def execute_mcp_node(
    db: AsyncSession, *, node: dict, upstream_outputs: dict, brief: dict, job_id: str
) -> dict:
    """Run one MCP node: resolve its server, render args, call the tool, persist
    the result as the node's output_text. Assumes the caller has already
    verified ``settings.mcp_tool_enabled``."""
    node_id = node["id"]
    node_key = node["node_key"]
    title = node.get("title")

    cfg = parse_tool_config(node.get("tool_config"))
    server_name = cfg.get("server")
    tool_name = cfg.get("tool")
    raw_args = cfg.get("args", {})

    if not server_name or not tool_name:
        return await _fail(
            db, node_id, node_key, title,
            "MCP node tool_config must specify both 'server' and 'tool' "
            f"(got server={server_name!r}, tool={tool_name!r})",
        )

    spec = await mcp_registry.get_server(db, server_name)
    if spec is None:
        return await _fail(
            db, node_id, node_key, title,
            f"MCP server {server_name!r} is not registered",
        )
    if not spec.enabled:
        return await _fail(
            db, node_id, node_key, title,
            f"MCP server {server_name!r} is disabled",
        )

    try:
        rendered = render_args(raw_args, upstream_outputs, brief)
        if not isinstance(rendered, dict):
            rendered = {}
        result = await mcp_client.call_tool(spec, tool_name, rendered)
    except mcp_client.McpError as exc:
        return await _fail(db, node_id, node_key, title, str(exc))

    output = result.text or "(MCP tool returned no content)"
    await db.execute(
        text(
            "UPDATE dag_nodes SET status = 'done', output_text = :o, "
            "completed_at = NOW() WHERE id = :nid"
        ),
        {"o": output, "nid": str(node_id)},
    )
    await db.commit()
    logger.info(
        "mcp_node_done: node=%s server=%s tool=%s chars=%d",
        node_key, server_name, tool_name, len(output),
    )
    return {
        "status": "done",
        "node_key": node_key,
        "title": title,
        "output": output,
        "passed": True,
        "verified": True,
        "confidence": 1.0,
        "model_used": f"mcp:{server_name}/{tool_name}",
        "tool": "MCP",
        "reason": "MCP tool executed",
    }
