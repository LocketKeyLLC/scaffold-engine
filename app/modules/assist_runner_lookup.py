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
        self._node_key: str | None = None

    def feed(self, name: str, data: dict[str, Any]) -> None:
        if name == "assist_guide_delta":
            if not self._guide_open:
                self._guide, self._guide_open = [], True
            self._guide.append(str(data.get("text") or ""))
        elif name == "assist_guide_done":
            self._last, self._guide_open = "".join(self._guide), False
            # §17.1166 — the step this reply was written FOR (§17.1149 stamps
            # it, since a repair step may have diverted the walkthrough).
            self._node_key = data.get("node_key") or self._node_key
        elif name == "assist_answer" and (data.get("kind") in ("fix", "guide")):
            self._last = str(data.get("text") or "")
            self._node_key = data.get("node_key") or self._node_key

    def text(self) -> str:
        return self._last

    def node_key(self) -> str | None:
        """§17.1166 — the step the look-up belongs to. Live (22:51:52): a
        walkthrough for ADD17 asked for `nvidia-smi`, the runner ran it, and
        the output re-entered the loop with NO step — the next reply landed on
        ADD65 and called the engine's own result "a red herring". The reply's
        own step beats the session pointer; the pointer is the fallback."""
        return self._node_key


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


# ---------------------------------------------------------------------------
# §17.1158 — what the runner ALREADY ran this session, so a fix that asks for
# it again is regenerated with the answer instead of re-requesting it. Live
# (ADD49): `qm guest cmd 110 network-get-interfaces` requested three times in
# ten minutes, `qm config 110` twice, the same ssh retried — each time the
# engine had the output on record and the gate could only warn.
# ---------------------------------------------------------------------------

_LEDGER_LINE_RE = re.compile(r"^\$ (.+?)\n(.*?)(?=^\$ |\Z)", re.S | re.M)

# §17.1166 — a REFUSAL is not an answer. The helper returns "(refused by the
# local runner: …)" as an ordinary tool result (not an MCP error), so
# `executed[].ok` is True and the text was the only tell — and `recent_lookups`
# recorded it as though the command had run. Live (ADD65, 23:04 UTC): the
# runner refused `sudo apt install -y qemu-guest-agent` (a mutation, correctly
# — see §17.1166's gate fix), the ledger filed it as known, and the redundancy
# gate then flagged the one CORRECT fix — "open the VM console and install the
# agent" — as "asks you to re-run … whose answer the engine already has".
# The gate argued against the only action that would have finished the step.
_NOT_AN_ANSWER_RE = re.compile(
    r"^\((?:refused by the local runner|runner error|timed out after)\b", re.I)


def is_answer(output: str) -> bool:
    """True when the recorded output is something the command actually
    PRINTED. A refusal, a transport error or a timeout means it never ran —
    the engine knows nothing more than before, so asking for it is not
    redundant. ``(no output)`` IS an answer: the command ran and matched
    nothing."""
    return not _NOT_AN_ANSWER_RE.match((output or "").strip())


async def recent_lookups(db, session_id: str, *, minutes: int = 180, limit_turns: int = 40) -> list[dict]:
    """``[{command, output, at}]`` from the session's ``[local-runner]``
    operator turns (look-ups and guest checks carry ``$ cmd`` + output),
    newest first; one entry per command (the newest wins)."""
    from sqlalchemy import text as _t
    try:
        rows = (await db.execute(_t("""
            SELECT content, created_at FROM assist_turns
             WHERE session_id = :sid AND role = 'operator' AND content LIKE '[local-runner]%'
               AND created_at > now() - make_interval(mins => :m)
             ORDER BY created_at DESC LIMIT :n
        """), {"sid": session_id, "m": int(minutes), "n": int(limit_turns)})).mappings().all()
    except Exception as exc:
        logger.warning("recent_lookups_failed sid=%s err=%r", session_id, exc)
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        for m in _LEDGER_LINE_RE.finditer(r["content"] or ""):
            cmd = " ".join(m.group(1).split())
            if cmd in seen:
                continue
            body = (m.group(2) or "").strip()
            if not is_answer(body):   # §17.1166 — refused/errored: never ran, nothing learned
                logger.info("runner_ledger_skipped_non_answer cmd=%r out=%r", cmd[:60], body[:60])
                continue
            seen.add(cmd)
            out.append({"command": cmd, "output": body, "at": r["created_at"], "by": "runner"})
    # §17.1159 — what the OPERATOR ran and pasted counts too: the same command
    # asked for again is redundant whether the runner or a person ran it.
    try:
        prows = (await db.execute(_t("""
            SELECT content, created_at FROM assist_turns
             WHERE session_id = :sid AND role = 'operator' AND kind IN ('submit', 'message')
               AND content NOT LIKE '[local-runner]%' AND created_at > now() - make_interval(mins => :m)
             ORDER BY created_at DESC LIMIT :n
        """), {"sid": session_id, "m": int(minutes), "n": int(limit_turns)})).mappings().all()
        from app.modules.assist_paste import parse_with_context, SENTINEL_ECHO_RE
        # §17.1167 — the walkthroughs/fixes this session issued, so a multi-line
        # paste can be split on the block it came from. Without them a block
        # paste files its LATER commands as the FIRST command's output.
        try:
            _grows = (await db.execute(_t("""
                SELECT content FROM assist_turns
                 WHERE session_id = :sid AND role = 'assistant' AND kind IN ('guide', 'fix')
                   AND created_at > now() - make_interval(mins => :m)
                 ORDER BY created_at DESC LIMIT 12
            """), {"sid": session_id, "m": int(minutes)})).scalars().all() or []
        except Exception as exc:
            logger.warning("recent_lookups_guidance_failed sid=%s err=%r", session_id, exc)
            _grows = []
        for r in prows:
            for e in parse_with_context(r["content"] or "", list(_grows)).entries:
                cmd = " ".join(e.command.split())
                if not cmd or cmd in seen or e.heredoc:
                    continue
                # §17.1167 — the copy button's own marker is not a look-up, and a
                # command inside a pasted BLOCK has no separable output: filing
                # either as "already answered" is how the gate learned to argue
                # against commands from output they never produced.
                if SENTINEL_ECHO_RE.match(cmd) or e.grouped:
                    continue
                if not is_answer(e.output or ""):   # §17.1166 — a refusal is not an answer on ANY path
                    continue
                seen.add(cmd)
                out.append({"command": cmd, "output": (e.output or "").strip(), "at": r["created_at"], "by": "operator"})
    except Exception as exc:
        logger.warning("recent_lookups_paste_failed sid=%s err=%r", session_id, exc)
    # §17.1166 — the two queries append runner entries then operator entries,
    # so an unsorted list buries every paste behind every look-up: a consumer
    # that takes the newest N (runner_ledger_block) would never reach what the
    # OPERATOR ran. One list, newest first.
    out.sort(key=lambda e: (e.get("at") is not None, e.get("at")), reverse=True)
    return out


def find_repeated_lookups(text_out: str, ledger: Optional[list[dict]]) -> list[dict]:
    """Commands in the draft's fenced blocks that the runner already ran (the
    ledger), as redundancy hits ``[{command, known, resource}]`` the fix gate
    understands: ``known`` names when it ran and what it printed."""
    if not ledger or not (text_out or "").strip():
        return []
    by_cmd = {e["command"]: e for e in ledger}
    hits: list[dict] = []
    seen: set[str] = set()
    for block in _FENCE_RE.findall(text_out or ""):
        for raw in block.splitlines():
            ln = " ".join(re.sub(r"^\$\s+", "", raw.strip()).split())
            e = by_cmd.get(ln)
            if not e or ln in seen:
                continue
            seen.add(ln)
            at = e.get("at")
            when = at.strftime("%H:%M UTC") if hasattr(at, "strftime") else str(at or "earlier")
            out = (e.get("output") or "(no output)").strip().replace("\n", " ⏎ ")
            who = "the operator ran it and pasted the result" if e.get("by") == "operator" else "the engine ran it through your local runner"
            hits.append({"command": ln, "resource": "",
                         "known": f"{who} at {when} and it printed: {out[:200]}"})
    return hits


def runner_ledger_block(ledger: Optional[list[dict]], *, limit: int = 12,
                        per_output: int = 400) -> str:
    """§17.1166 — the prompt block that stops a WALKTHROUGH from asking for
    what the engine already has.

    §17.1158 folded the ledger into `generate_fix`'s gate only, and recorded
    "Not done: guides do not consult the ledger". Live (ADD65, 2026-09-22):
    the runner ran `qm status 106` at 22:53:19 and printed `status: stopped`;
    the guide 34 seconds later opened with "Run this now: `qm status 106` —
    then tell me what it shows". A guide is flag-don't-regen (§17.887), so the
    fix has to land BEFORE the draw: give the walkthrough the answers, and it
    has nothing to ask for."""
    if not ledger:
        return ""
    lines: list[str] = []
    for e in ledger[:limit]:
        at = e.get("at")
        when = at.strftime("%H:%M UTC") if hasattr(at, "strftime") else str(at or "earlier")
        who = "the operator ran it" if e.get("by") == "operator" else "the engine ran it on the target machine"
        out = (e.get("output") or "(no output)").strip()
        if len(out) > per_output:
            out = out[:per_output] + " …"
        lines.append(f"$ {e['command']}   ({who}, {when})\n{out}")
    return ("ALREADY RUN THIS SESSION — these commands and their real output are "
            "on file. Do NOT ask the operator to run any of them again, and do not "
            "ask for output you can read here. Use these values. If they do not "
            "settle the question, ask for something DIFFERENT that would:\n\n"
            + "\n\n".join(lines))
