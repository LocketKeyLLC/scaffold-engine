"""§17.1077 — the opt-in local runner: the state check runs its own probes.

The engine's founding rule is that it never touches the operator's machine:
every probe is a paste round-trip, and a 48-probe state check is that rule's
cost made visible. This module is the ONE crossing, and it is fenced:

- OFF unless ``assist_local_runner_server`` names a registered MCP server
  (`mcp_servers`) that the operator runs on their own machine
  (`scripts/local_runner_mcp.py`, read-only by construction) — a decision
  the operator makes per deployment, not a default.
- Every command is checked by the engine's own read-only gate
  (`read_only_command`, AST-based) BEFORE it is sent, and the runner
  refuses anything but the same read-only shapes on its side too.
- Every executed command and its output is recorded in the transcript as an
  operator turn marked ``[local-runner]`` — the same durable record a paste
  would have left, so nothing runs that the transcript does not show.
- It only ever runs STATE-CHECK PROBES. Walkthrough commands stay the
  operator's hands.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("scaffold")

MARKER = "[local-runner]"


async def runner_spec(db):
    """The configured runner's registry spec, or None when the feature is off."""
    from app.config import settings
    name = (settings.assist_local_runner_server or "").strip()
    if not name:
        return None
    from app.modules.mcp_registry import get_server
    spec = await get_server(db, name)
    if spec is None or not spec.enabled:
        logger.warning("local_runner_not_available name=%s (unregistered or disabled)", name)
        return None
    return spec


async def run_probes(spec, probes: list[dict], *, on_progress=None) -> tuple[str, list[dict]]:
    """Execute the probes through the runner and return ``(pasted, executed)``
    where ``pasted`` is exactly the marker-attributed text a human paste
    would have produced (so `resolve_state_check` needs no new path)."""
    from app.modules.assist_state_check import read_only_command
    from app.modules.mcp_client import call_tool

    chunks: list[str] = []
    executed: list[dict] = []
    seen: dict[str, tuple[str, bool]] = {}   # identical commands run once
    for i, p in enumerate(probes, 1):
        cmd = p["command"]
        if not read_only_command(cmd):  # belt and braces — plan_probes already gated
            logger.warning("local_runner_refused id=%s cmd=%r", p["id"], cmd[:80])
            continue
        if cmd in seen:
            out, ok = seen[cmd]
        else:
            try:
                res = await call_tool(spec, "run_readonly", {"command": cmd, "timeout_s": 20})
                out = _plain_output(res)
                ok = not res.is_error
            except Exception as exc:  # noqa: BLE001 — one probe failing must not sink the check
                out, ok = f"(runner error: {exc})", False
            seen[cmd] = (out, ok)
        chunks.append(f'== {p["id"]} ==\n{out.rstrip()}\n')
        executed.append({"id": p["id"], "command": cmd, "ok": ok, "chars": len(out)})
        if on_progress is not None:
            try:
                await on_progress(i, len(probes))
            except Exception:  # noqa: BLE001
                pass
    return "".join(chunks), executed


def _plain_output(res) -> str:
    """The runner returns one string; the MCP client prefers the structured
    envelope (``{"result": "…"}``) when the server emits one. The paste must
    be the shell output itself, not a JSON wrapper around it."""
    st = getattr(res, "structured", None)
    if isinstance(st, dict) and isinstance(st.get("result"), str):
        return st["result"]
    return res.text or ""


def transcript_record(executed: list[dict], pasted: str) -> str:
    """What goes into the transcript as the operator turn: the marker, the
    commands, and the output — the same thing a paste would have been."""
    head = f"{MARKER} the state check ran {len(executed)} read-only probe(s) through your local runner:\n"
    cmds = "\n".join(f"  {e['id']}: {e['command']}" for e in executed)
    return head + cmds + "\n\n" + pasted
