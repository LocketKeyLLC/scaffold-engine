"""§17.1205 — what a runner has actually been asked to do, so the operator can
watch it.

The Machines page could connect a machine and test the connection, and that was
all: no way to see what the engine had been running there. The record existed
only as prose inside `[local-runner]` assist turns — three different opening
sentences across 28 turns on this database — so reading it back meant writing a
parser per phrasing, and every new phrasing would drop rows silently.

So the commands are recorded where they are SENT, which is one place:
`mcp_client.call_tool`. Not at the callers (`run_probes`, `run_block`, the
walkthrough look-ups, the guest checks), because that is four sites that would
drift apart — the shape §17.854/975/976/979/984 kept arriving as.

In memory, deliberately. The orchestrator is a single uvicorn worker (no
`workers=` in `app.run_server`), so one module-level buffer sees every call.
A restart loses it, which is why the surface says "since the engine started"
rather than implying a durable log: this answers "what is it doing / what did
it just do", and a claim that outlives the process would need a table and a
retention sweep for a panel that is inherently about the recent past.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("scaffold")

#: The tools that carry an operator-visible COMMAND. `write_policy` and the tool
#: listings are how the engine asks a runner about itself, not work it did, and
#: the probe calls those on every page load — they would bury the real rows.
#: §17.1274 — `write_file` (§17.1271) carries a path and content, and is a write.
COMMAND_TOOLS = ("run_readonly", "run_supervised", "write_file")

#: Bounded: this is "recently", not a log. ~200 entries covers a full state
#: check (the live ones ran 23 and 24 probes) several times over.
MAX_ENTRIES = 200

_entries: deque[dict] = deque(maxlen=MAX_ENTRIES)
_seq = 0


def record(*, runner: str, tool: str, command: str, output: str = "", ran: bool = True,
           why: str = "", ms: Optional[int] = None) -> None:
    """One sent command. Never raises: a recording failure must not break the
    call it is describing."""
    global _seq
    try:
        _seq += 1
        _entries.appendleft({
            "seq": _seq,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "runner": runner,
            "tool": tool,
            "kind": "write" if tool in ("run_supervised", "write_file") else "read",
            "command": (command or "")[:400],
            # The first line is what a list can show; the body is not kept, so
            # this never becomes a second copy of the machine's output.
            "first_line": _first_line(output),
            "lines": len((output or "").splitlines()),
            "ran": bool(ran),
            "why": why or "",
            "ms": ms,
        })
    except Exception as exc:                       # pragma: no cover - defensive
        logger.warning("runner_activity_record_failed err=%r", exc)


def _first_line(output: str) -> str:
    for ln in (output or "").splitlines():
        if ln.strip():
            return ln.strip()[:160]
    return ""


def recent(*, runner: Optional[str] = None, limit: int = 40) -> list[dict]:
    """Newest first. `runner` filters to one machine's rows."""
    out = [e for e in _entries if runner is None or e["runner"] == runner]
    return out[:max(1, min(int(limit or 40), MAX_ENTRIES))]


def summary(*, runner: Optional[str] = None) -> dict:
    """What the page needs beside the list: how much there is, and whether the
    runner is mid-command right now (`started` without a matching record)."""
    rows = recent(runner=runner, limit=MAX_ENTRIES)
    return {
        "count": len(rows),
        "refused": sum(1 for e in rows if not e["ran"]),
        "last_at": rows[0]["at"] if rows else None,
        "running": _inflight_count(runner),
        # said plainly on the surface: this is not a durable log
        "since": _STARTED,
    }


_STARTED = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

#: Commands sent and not yet finished, so the page can say "running" honestly
#: rather than inferring it from a timestamp.
_inflight: dict[int, tuple[str, float]] = {}
_inflight_seq = 0


def started(runner: str) -> int:
    global _inflight_seq
    _inflight_seq += 1
    _inflight[_inflight_seq] = (runner, time.monotonic())
    return _inflight_seq


def finished(token: int) -> Optional[int]:
    """Elapsed ms for a call `started()` opened, and clears it."""
    hit = _inflight.pop(token, None)
    return int((time.monotonic() - hit[1]) * 1000) if hit else None


def _inflight_count(runner: Optional[str]) -> int:
    return sum(1 for r, _ in _inflight.values() if runner is None or r == runner)


def reset() -> None:
    """Tests only."""
    global _seq, _inflight_seq
    _entries.clear()
    _inflight.clear()
    _seq = _inflight_seq = 0


def _command_of(tool: str, args: dict) -> str:
    """What the list shows for one call. A command tool carries `command`; the
    §17.1271 file tool carries a path and CONTENT, and the content is the one
    thing this list must never keep (§17.1205: not a second copy of the
    machine), so a write reads `write_file <path> (<n> bytes)`."""
    a = args or {}
    if tool == "write_file":
        body = str(a.get("content") or "")
        return f"write_file {a.get('path') or '?'} ({len(body.encode('utf-8', 'replace'))} bytes)"
    return str(a.get("command") or "")


def note_result(spec: Any, tool_name: str, args: dict, out: Any, token: Optional[int]) -> None:
    """The hook `mcp_client.call_tool` calls on the way OUT, for a tool that
    carries a command. Reads the runner's own refusal vocabulary, so a command
    the runner would not run is not listed as one that ran (§17.1204)."""
    from app.modules.assist_local_runner import not_evidence
    try:
        st = getattr(out, "structured", None)
        text_out = st["result"] if isinstance(st, dict) and isinstance(st.get("result"), str) else (out.text or "")
    except Exception:
        text_out = ""
    why = not_evidence(text_out, bool(getattr(out, "is_error", False)))
    record(runner=getattr(spec, "name", "?") or "?", tool=tool_name,
           command=_command_of(tool_name, args), output=text_out,
           ran=not why, why=why, ms=finished(token) if token is not None else None)


def note_failure(spec: Any, tool_name: str, args: dict, exc: BaseException,
                 token: Optional[int]) -> None:
    """A call that never came back. §17.1201 — that is not a command result and
    must not read as one, so it is recorded as a command that did not run."""
    record(runner=getattr(spec, "name", "?") or "?", tool=tool_name,
           command=_command_of(tool_name, args), output="",
           ran=False, why=f"the call to the runner did not come back ({type(exc).__name__})",
           ms=finished(token) if token is not None else None)
