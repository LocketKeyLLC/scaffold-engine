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

#: §17.1557 — the documented forms `${upstream.T2}` / `${brief.x.y}` AND the bare `${T2}` the planner
#: actually writes (live, job 263e920a: every MCP node carried `${T1}`/`${T2}`/`${T3}`, which rendered
#: as nothing and ran as empty shell variables). A bare `T<n>`/`T<n>v<m>` renders ONLY when that key
#: is an upstream node; any other `${…}` stays verbatim (fail-visible, as before).
_PLACEHOLDER = re.compile(r"\$\{\s*(?:(upstream|brief)\.([A-Za-z0-9_.\-]+)|(T\d+(?:v\d+)?))\s*\}")

#: §17.1556 — an answer that is nothing but the shell complaining is not a result. The runner's
#: read-only tool returns stdout+stderr as one string with no exit code, so a `grep` of a path that
#: does not exist (`grep: …: No such file or directory`) or a loop the shell could not parse
#: (`/bin/sh: 1: Syntax error: "do" unexpected`) came back as text and the node was marked done
#: (live: T1/T2/T3 of job 263e920a, all "done", all errors). Judged only when EVERY non-empty line
#: is such a complaint — a real result with one warning line is still a result.
_SHELL_ERROR_LINE = re.compile(
    r"^(?:(?:/usr)?/bin/)?(?:sh|bash|dash|zsh): (?:\d+: |line \d+: )?.*(?:not found|Syntax error|No such file or directory|Permission denied)"
    r"|^[A-Za-z0-9_.+-]+: (?:.+: )?(?:No such file or directory|command not found|Permission denied|not found|Is a directory)\.?$"
    r"|^curl: \(\d+\) .*$")


def only_shell_errors(text_out: str) -> bool:
    """§17.1556 — True when the output has lines and every one of them is a shell/tool error."""
    lines = [ln.strip() for ln in (text_out or "").splitlines() if ln.strip()]
    return bool(lines) and all(_SHELL_ERROR_LINE.search(ln) for ln in lines)


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
            if m.group(3):                           # §17.1557 — bare `${T2}`: an upstream key, or verbatim
                val = _lookup("upstream", m.group(3), upstream, brief)
            else:
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
    # §17.1107 (ledger S-5) — guarded on 'running': a node the reaper or a
    # cancel already reset is not overwritten by this stale result.
    res = await db.execute(
        text(
            "UPDATE dag_nodes SET status = 'failed', "
            "last_verification_reason = :r, completed_at = NOW() "
            "WHERE id = :nid AND status = 'running' RETURNING id"
        ),
        {"r": reason, "nid": str(node_id)},
    )
    if res.fetchone() is None:
        logger.warning("node_status_write_refused: node=%s to=failed expected=running", node_id)
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

    # §17.1551 — a machine-write MCP node must NOT run deterministically: the runner's write tool
    # demands a single-use engine↔runner approval, and minting one here (with no operator pause) would
    # bypass the write-approval gate and put any inlined secret in the sudo log. Such a node parks for
    # approval instead (supervised_runs.is_mcp_write → pending_hands_on → _pause_for_decision), and the
    # approved path mints the approval and runs it. Reaching here means that pause was bypassed — fail
    # clearly rather than call the tool unapproved.
    from app.modules.assist_supervised import WRITE_TOOL, WRITE_FILE_TOOL
    if tool_name in (WRITE_TOOL, WRITE_FILE_TOOL):      # §17.1573 — the file write is a write too
        return await _fail(
            db, node_id, node_key, title,
            f"a machine-write MCP node (tool {tool_name!r}) runs through the operator-approved "
            "supervised pause, not as a deterministic MCP call; it reached execution without that "
            "pause (§17.1551/§17.1573)",
        )

    try:
        rendered = render_args(raw_args, upstream_outputs, brief)
        if not isinstance(rendered, dict):
            rendered = {}
        result = await mcp_client.call_tool(spec, tool_name, rendered)
    except mcp_client.McpError as exc:
        return await _fail(db, node_id, node_key, title, str(exc))

    # §17.1556 — the runner's answer, unwrapped from its `{"result": "…"}` envelope, and judged: a tool
    # error, a runner refusal/timeout/privilege note, or an output that is nothing but shell errors is a
    # FAILED node, not a done one. Live, T1/T2/T3 of job 263e920a all marked done on
    # `grep: …: No such file or directory` / `/bin/sh: 1: Syntax error: "do" unexpected`, and every
    # step after them spliced those strings in as data.
    from app.modules import assist_local_runner as _lr
    output = _lr._plain_output(result) or "(MCP tool returned no content)"
    why = _lr.not_evidence(output, getattr(result, "is_error", False) is True)
    if not why and tool_name == "run_readonly" and only_shell_errors(output):
        why = "the machine answered only with shell errors, not a result"
    if why and tool_name == "run_readonly" and why.startswith("the runner is not allowed to read this"):
        # §17.1574b — the READ needs root (the runner is unprivileged on the read channel: live, T8 of
        # 9e1c2c68 `pve-firewall status; pve-firewall compile` → "ipcc_send_rec failed"). That is not a
        # failed step — it is a read the OPERATOR may approve on the root channel. Mark the node so the
        # frontier holds it (`mcp_needs_the_pause` → `needs_root`), put it back to pending, and report
        # `needs_approval` like any hands-on step; the drained loop frames it with its own command.
        cfg2 = dict(cfg); cfg2["needs_root"] = True
        await db.execute(
            text("UPDATE dag_nodes SET tool_config = CAST(:cfg AS jsonb), status = 'pending', started_at = NULL, "
                 "last_verification_reason = :r, updated_at = NOW() WHERE id = :nid AND status = 'running'"),
            {"cfg": json.dumps(cfg2), "nid": str(node_id),
             "r": f"the read-only channel refused this read for privilege (§17.1574b); it now asks for approval to run as root: {output.strip()[:200]}"})
        await db.commit()
        logger.info("mcp_read_needs_root node=%s cmd=%r", node_key, str((rendered or {}).get("command") or "")[:90])
        return {"status": "needs_approval", "node_key": node_key, "title": title, "tool": "MCP",
                "reason": "the read needs root: handed back for approval (§17.1574b)"}
    if why:
        return await _fail(db, node_id, node_key, title,
                           f"{why} (§17.1556): {output.strip()[:400]}")
    res = await db.execute(
        text(
            "UPDATE dag_nodes SET status = 'done', output_text = :o, "
            "completed_at = NOW() WHERE id = :nid AND status = 'running' RETURNING id"
        ),
        {"o": output, "nid": str(node_id)},
    )
    if res.fetchone() is None:
        logger.warning("node_status_write_refused: node=%s to=done expected=running", node_id)
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
