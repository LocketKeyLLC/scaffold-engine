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


async def runner_spec(db, *, include_disabled: bool = False):
    """The configured runner's registry spec, or None when the feature is off.

    §17.1205 — `include_disabled` is for the ONE caller that needs a paused
    runner: the thing that un-pauses it. Everything else must keep seeing None
    for a disabled row, because that is what "the engine sends this machine
    nothing" means. A second resolver would have drifted from this one, so it is
    a flag on the single rule instead.
    """
    from app.config import settings
    name = (settings.assist_local_runner_server or "").strip()
    if not name:
        # §17.1146 — a runner the ASSIST registered (from the in-plan recipe)
        # is tagged on its registry row; no .env edit or restart needed.
        try:
            from app.modules.engine_setup import LOCAL_RUNNER_MARK
            from app.modules.mcp_registry import list_servers
            for spec in await list_servers(db, include_disabled=include_disabled):
                if (spec.enabled or include_disabled) and (spec.description or "").startswith(LOCAL_RUNNER_MARK):
                    return spec
        except Exception as exc:
            logger.warning("local_runner_tagged_lookup_failed err=%r", exc)
        return None
    from app.modules.mcp_registry import get_server
    spec = await get_server(db, name)
    if spec is None or not (spec.enabled or include_disabled):
        logger.warning("local_runner_not_available name=%s (unregistered or disabled)", name)
        return None
    return spec


#: §17.1204 — what a runner says when it did NOT run the command. None of these
#: is output about the operator's system, and the state-check judge reads a
#: section body as exactly that. The privilege note says so in its own words:
#: "a permission refusal, not a statement about your system" — live (§17.1202),
#: an unprivileged runner on a Proxmox host answered every `qm`/`pct` check with
#: `Unable to load access control list`, and that is what the judge was ruling on.
_NOT_EVIDENCE = (
    ("(refused by the local runner", "the runner refused it"),
    ("(runner error:", "the call to the runner did not come back"),
    ("(timed out after", "it timed out on the machine"),
    ("(the runner is UNPRIVILEGED", "the runner is not allowed to read this"),
)


def not_evidence(text_out: str, is_error: bool = False) -> str:
    """Why this probe says nothing about the system, or ``''`` when it ran."""
    head = (text_out or "").lstrip()
    for prefix, why in _NOT_EVIDENCE:
        if head.startswith(prefix):
            return why
    return "the runner reported an error" if is_error else ""


async def run_probes(spec, probes: list[dict], *, on_progress=None) -> tuple[str, list[dict]]:
    """Execute the probes through the runner and return ``(pasted, executed)``
    where ``pasted`` is exactly the marker-attributed text a human paste
    would have produced (so `resolve_state_check` needs no new path)."""
    from app.modules.assist_state_check import read_only_command
    from app.modules.mcp_client import call_tool

    chunks: list[str] = []
    executed: list[dict] = []
    seen: dict[str, tuple[str, str]] = {}   # identical commands run once
    for i, p in enumerate(probes, 1):
        cmd = p["command"]
        if not read_only_command(cmd):  # belt and braces — plan_probes already gated
            logger.warning("local_runner_refused id=%s cmd=%r", p["id"], cmd[:80])
            continue
        if cmd in seen:
            out, why = seen[cmd]
        else:
            try:
                res = await call_tool(spec, "run_readonly", {"command": cmd, "timeout_s": 20})
                out = _plain_output(res)
                why = not_evidence(out, bool(res.is_error))
            except Exception as exc:
                out, why = f"(runner error: {exc})", "the call to the runner did not come back"
            seen[cmd] = (out, why)
        # §17.1204 — ONLY real output becomes a section. `judge_outputs` reads a
        # section body as the command's answer, and a present-but-empty one as
        # "it printed nothing" — which is itself evidence. A refusal is neither:
        # writing it under the marker asks the judge to rule on a claim from a
        # sentence about the runner. An absent marker is the one shape that
        # means "unknown", which is the truth here.
        if not why:
            chunks.append(f'== {p["id"]} ==\n{out.rstrip()}\n')
        else:
            logger.warning("local_runner_no_evidence id=%s why=%s cmd=%r", p["id"], why, cmd[:80])
        executed.append({"id": p["id"], "command": cmd, "ok": not why, "ran": not why,
                         "why": why, "output": out.rstrip(), "chars": len(out)})
        if on_progress is not None:
            try:
                await on_progress(i, len(probes))
            except Exception:
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
    ran = [e for e in executed if e.get("ran", True)]
    head = f"{MARKER} the state check ran {len(ran)} read-only probe(s) through your local runner:\n"
    cmds = "\n".join(f"  {e['id']}: {e['command']}" for e in ran)
    # §17.1204 — a probe that produced no evidence is left out of the output
    # above on purpose, so it is judged "unknown" rather than ruled on from a
    # refusal. Left out silently it would look like it had simply passed, so it
    # is named here with the reason and the runner's own words.
    blocked = [e for e in executed if not e.get("ran", True)]
    tail = ""
    if blocked:
        # The TAIL of the output, not the head: the privilege note is ~250
        # characters of fixed boilerplate and the line that says what actually
        # happened (`Unable to load access control list`) comes after it.
        tail = ("\n\nThese could not be checked through the runner, so they stay unknown:\n"
                + "\n".join(f"  {e['id']}: {e['command']} — {e.get('why') or 'it did not run'}"
                            f"{chr(10) + '    ' + (e.get('output') or '')[-200:] if e.get('output') else ''}"
                            for e in blocked))
    return head + cmds + "\n\n" + pasted + tail
