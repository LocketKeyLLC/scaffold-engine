"""§17.1150 — the connected local runner carries the engine's OWN read-only
look-ups.

Live (T6, 21:08 UTC, minutes after the runner was proven reachable): the
walkthrough opened with "Run this now: cat /etc/pve/firewall/host.fw;
pve-firewall status; iptables -S PVEFW-HOST-IN — then tell me what it
shows", and the fix that followed asked for `ls /etc/pve/firewall/groups/`.
Both are read-only look-ups the engine wanted for ITSELF; both went to the
operator as paste requests. "It acknowledged the connection then acted as
though it didn't exist."

§17.1077's fence was "state-check probes only; walkthrough commands stay the
operator's hands". The fence that matters is READ-ONLY, and it is enforced at
both ends (`read_only_command` here, the runner's own gate there): a block the
engine asks the operator to run and report back, made only of read-only
commands, is a look-up the engine can do itself. Anything that writes is still
the operator's — the gate refuses it and the block stays a paste request.

One operator turn allows at most ``MAX_AUTO_ROUNDS`` automatic round trips
(look-up → the output re-enters the loop as the paste would have → the next
guide/fix), so a walkthrough that keeps asking for look-ups converges instead
of looping.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("scaffold")

MARK = "[local-runner]"
MAX_AUTO_ROUNDS = 2
_FENCE_RE = re.compile(r"```(?:bash|sh|shell)?[ \t]*\n(.*?)```", re.S)
_RUN_NOW_RE = re.compile(r"\*\*Run this now:?\*\*|👉 Do this next", re.I)


def first_lookup_block(text: str) -> Optional[str]:
    """The FIRST fenced shell block the reply asks the operator to run: the
    one under **Run this now** / 👉 Do this next, else the first fence at all.
    None when the reply carries no fence."""
    t = text or ""
    m = _RUN_NOW_RE.search(t)
    start = m.end() if m else 0
    f = _FENCE_RE.search(t, start) or (_FENCE_RE.search(t) if m else None)
    return f.group(1).strip() if f else None


def lookup_lines(block: str) -> tuple[list[str], list[str]]:
    """``(runnable, refused)``: each non-empty, non-comment line of the block
    judged by the engine's read-only gate. A block is a look-up only when
    NOTHING in it is refused."""
    from app.modules.assist_state_check import read_only_command
    runnable: list[str] = []
    refused: list[str] = []
    for raw in (block or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        ln = re.sub(r"^\$\s+", "", ln)                     # a copied prompt marker
        if re.match(r"^(cd|export|set|unset)\b", ln):     # shell state, nothing to observe
            continue
        (runnable if read_only_command(ln) else refused).append(ln)
    return runnable, refused


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_SKIP_SECTION_RE = re.compile(r"rollback|undo|revert|if (?:something|anything) (?:went|goes) wrong", re.I)
MAX_LOOKUP_BLOCKS = 6
MAX_LOOKUP_COMMANDS = 24


def lookup_blocks(text: str) -> list[str]:
    """§17.1156 — the reply's LEADING read-only blocks, in reading order,
    starting at the **Run this now** block: each block is included while it
    is purely read-only; the first block that writes ends the scan (every
    block after it depends on that change); blocks under a Rollback/Undo
    heading are skipped; a block the guide repeats verbatim runs once."""
    t = text or ""
    m = _RUN_NOW_RE.search(t)
    start = m.end() if m else 0
    fences = list(_FENCE_RE.finditer(t, start)) or (list(_FENCE_RE.finditer(t)) if m else [])
    headings = [(h.start(), h.group(1)) for h in _HEADING_RE.finditer(t)]
    out: list[str] = []
    seen: set[str] = set()
    for f in fences:
        head = next((title for pos, title in reversed(headings) if pos < f.start()), "")
        if _SKIP_SECTION_RE.search(head or ""):
            continue
        block = f.group(1).strip()
        if not block or block in seen:
            continue
        runnable, refused = lookup_lines(block)
        if refused:
            logger.info("runner_lookup_stop_at_write first=%r", refused[0][:80])
            break
        if not runnable:
            continue
        seen.add(block)
        out.append(block)
        if len(out) >= MAX_LOOKUP_BLOCKS:
            break
    return out


def is_lookup(text: str) -> Optional[list[str]]:
    """The commands the engine can run itself for this reply, or None when
    the reply's first block is not purely read-only (or there is no block)."""
    if not first_lookup_block(text):
        return None
    blocks = lookup_blocks(text)
    if not blocks:
        return None
    commands: list[str] = []
    for b in blocks:
        commands.extend(lookup_lines(b)[0])
    return commands[:MAX_LOOKUP_COMMANDS] or None


async def run_lookup(spec, commands: list[str], *, on_progress=None) -> tuple[str, list[dict]]:
    """Execute the look-up through the runner (the same executor and re-gate
    the state check uses). Returns ``(output_text, executed)``."""
    from app.modules import assist_local_runner as _lr
    probes = [{"id": f"L{i}", "command": c} for i, c in enumerate(commands, 1)]
    pasted, executed = await _lr.run_probes(spec, probes, on_progress=on_progress)
    return pasted, executed


def record_text(spec_name: str, executed: list[dict], pasted: str) -> str:
    """The operator turn the look-up becomes — what a paste would have been,
    marked so the transcript shows the engine ran it. Output sections are
    re-keyed by command (the state-check ids mean nothing here)."""
    out: list[str] = [f"{MARK} ran the walkthrough's read-only look-up through your local runner ({spec_name}) — "
                      f"these commands ran ON THE TARGET MACHINE itself, in the runner's own shell there, not in a sandbox:"]
    sections = dict(re.findall(r"^== (L\d+) ==\n(.*?)(?=^== L\d+ ==\n|\Z)", pasted or "", re.S | re.M))
    for e in executed:
        body = (sections.get(e["id"]) or "").rstrip()
        out.append(f"$ {e['command']}\n{body if body else '(no output)'}")
    return "\n".join(out)


def render_note(spec_name: str, executed: list[dict], pasted: str) -> str:
    """The ephemeral bubble the operator sees while it happens (the durable
    copy is the operator turn)."""
    body = record_text(spec_name, executed, pasted).split("\n", 1)[1] if executed else ""
    body = body if len(body) <= 6000 else body[:6000] + "\n… (truncated here; the full output is in the transcript)"
    hint = ""
    if "refused by the local runner" in (pasted or ""):
        hint = ("\n\n_Your runner's helper is older than the engine's read-only rules — re-run the install line from "
                "the local runner step (same token) to refresh it; until then it refuses some read-only forms._")
    return (f"🔁 Your local runner is connected, so I ran that look-up myself ({len(executed)} read-only "
            f"command{'s' if len(executed) != 1 else ''} through {spec_name}):\n\n```\n{body}\n```" + hint)


class ReplyTail:
    """Collects, from one turn's event stream, the text of the LAST reply
    that could carry a look-up block: the streamed guide, or a fix/guide/ask
    answer. Later replies replace earlier ones."""

    def __init__(self) -> None:
        self._guide: list[str] = []
        self._last: str = ""
        self._guide_open = False

    def feed(self, name: str, data: dict[str, Any]) -> None:
        if name == "assist_guide_delta":
            if not self._guide_open:
                self._guide, self._guide_open = [], True
            self._guide.append(str(data.get("text") or ""))
        elif name == "assist_guide_done":
            self._last, self._guide_open = "".join(self._guide), False
        elif name == "assist_answer" and (data.get("kind") in ("fix", "guide")):
            self._last = str(data.get("text") or "")

    def text(self) -> str:
        return self._last


_ON_RE = re.compile(r"📍\s*On:\s*(.+)")
_NOT_RUNNER_WORDS = ("console", "novnc", "web ui", "browser", "inside", "guest", "container", "lxc", " ct ", " vm ",
                     "vm's", "virtual machine", "the vm", "windows", "your laptop", "your pc", "your workstation")


def block_location(text: str) -> Optional[str]:
    """The `📍 On:` line that governs the reply's FIRST block (the nearest one
    before it, else the first one anywhere), or None."""
    t = text or ""
    m = _RUN_NOW_RE.search(t)
    start = m.end() if m else 0
    f = _FENCE_RE.search(t, start) or (_FENCE_RE.search(t) if m else None)
    before = t[:f.start()] if f else t
    ons = _ON_RE.findall(before)
    if ons:
        return ons[-1].strip()
    ons = _ON_RE.findall(t)
    return ons[0].strip() if ons else None


def location_is_runner_host(where: Optional[str], *, host: Optional[str], ip: Optional[str], user: Optional[str]) -> bool:
    """§17.1152 — True when the block is meant for the machine the runner is
    on. No location line → the shell (True). A console / a VM / a container /
    another `user@host` → False."""
    if not where:
        return True
    w = f" {where.lower()} "
    if any(k in w for k in _NOT_RUNNER_WORDS):
        return False
    mm = re.findall(r"\b([a-z_][a-z0-9_-]*)@([a-z0-9][a-z0-9.-]*)", w)
    if mm:
        ok_hosts = {str(h).lower() for h in (host, ip) if h and not str(h).startswith("<")}
        return any(h.lower() in ok_hosts or any(h.lower().startswith(o.split(".")[0]) for o in ok_hosts) for _u, h in mm)
    return True
