"""§17.1183 — a step is hands-on because of what it DOES, not the tool tag it
was drafted with.

The §17.624 hands-on gate counted tool tags: a `Shell` step with no shell
backend cannot be run autonomously, so a plan that is mostly Shell parks for
/assist instead of fabricating "done". Live (the home-lab job, 2026-09-27): the
assist engine's repairs inserted 93 steps and tagged 78 of them `LLM` — "Install
the NVIDIA driver 580.173.02 on the Proxmox host", "Install and enable QEMU
Guest Agent in VM 106", "Expand VM 106 disk to 100GB" — so the gate saw 37%
Shell on a plan that is nearly all host work, and "▶ Let the engine run it"
would have written 22 runbooks and marked the job complete.

Three structural signals decide instead, none of them one operator's stack:

* a command in the step text whose parsed head is a known MUTATION (the
  §17.1150 read-only classifier's own verb table, applied to structure) —
  a step that tells someone to `tee -a` a file changes a machine;
* a command the step names as how its completion is OBSERVED ("Done when
  `qm agent 106 ping` returns …", "verify with `pct status 111`") — completion
  is read off a machine the executor cannot reach;
* an explicit `user@host` target alongside a command.

Fail-open by construction (§17.1165): a command the AST cannot parse is NOT
evidence of anything. Tools with a real executor (CodeGen in the sandbox,
SearXNG, Milvus) keep their own classification.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("scaffold")

_FENCE_RE = re.compile(r"```[a-zA-Z]*[ \t]*\n(.*?)```", re.S)
# `qm agent 106 ping` / 'ip -brief link show veth105i0' — a quoted run of text
# that starts like a command name and has at least one argument or flag.
_INLINE_RE = re.compile(r"[`']([a-z][\w.+-]*(?:\s+[^`'\n]{1,150})?)[`']")
_OBSERVE_RE = re.compile(r"\b(?:done when|verif(?:y|ied|ies)|confirm(?:s|ed)?|check(?:s|ed)? that|reports?|returns?|prints?|shows?)\b", re.I)
# An UNQUOTED command in prose — "(pct set 101 --nameserver 192.168.1.1)",
# "via pct set, then reboot", "(iptables -L PVEFW-HOST-IN -n -v | grep 8790)".
# Only a head from the state-check's subcommand table counts, and only when
# its next token is an option or a verb the same tables know — "make sure
# the ip address" and "kill the process" are prose.
_BARE_RE = re.compile(r"(?<![\w/.@-])([a-z][\w.-]*)(?=\s)")
_BARE_REST_RE = re.compile(r"\s+((?:-{1,2}[\w-]+|[a-z][\w-]*)(?:[^,.;()`'\n]{0,150})?)")
_READ_VERBS = frozenset({"status", "config", "list", "show", "exec", "agent", "ps", "logs", "inspect"})
_HOST_RE = re.compile(r"(?<![\w.])[a-z_][\w-]*@[a-z0-9][\w-]*(?:\.[\w-]+)*(?![\w.])", re.I)
_EXECUTABLE_TOOLS = frozenset({"codegen", "searxng", "milvus"})


def command_writes(cmd: str) -> Optional[bool]:
    """True when the command DEFINITELY changes something (a mutation head,
    a redirect to a file, a pipe into an interpreter); False when it parsed
    and is not; None when the AST could not parse it — no evidence."""
    from app.modules.shell_ast import analyze
    from app.modules.assist_state_check import (
        _MUTATION_RE, _SUBCOMMAND_HEADS, container_exec_remainder, privilege_wrapper_remainder,
    )
    c = (cmd or "").strip()
    if not c or c.startswith("#"):
        return None
    facts = analyze(c)
    if facts.parse_error or not facts.commands:
        return None
    if facts.redirect_targets or facts.piped_to_interpreter:
        return True
    for argv, head in zip(facts.commands, facts.heads, strict=True):
        inner = privilege_wrapper_remainder(argv)
        if inner is None:
            inner = container_exec_remainder(argv)
        if inner:
            w = command_writes(inner)
            if w:
                return True
            continue
        probe = head if argv[0] in _SUBCOMMAND_HEADS else argv[0]
        if _MUTATION_RE.search(" " + probe + " "):
            return True
    return False


#: §17.1253 — a sentence that names a command in order to RULE IT OUT.
#:
#: The classifier reads a step's description for commands and takes any write as
#: proof the step does host work. It cannot tell a command being performed from
#: one being forbidden, and a correction written into a description is usually
#: phrased as a prohibition. Live, ADD112 ("point the Spectrum router's DNS at
#: Pi-hole") is work the operator does in an APP — it had been producing prose
#: correctly — until a note was added saying "do NOT run `pct exec 130 --
#: /usr/local/bin/pihole -a enabledhcp`". That one quoted command flipped the
#: step to hands-on, so instead of a walkthrough it was claimed and handed back
#: for approval, and the operator got nothing.
#:
#: Only a sentence counts here, so a FENCED command — the step's actual work —
#: is never excused: `step_commands` gives fenced lines an empty sentence.
_FORBIDDEN = re.compile(
    r"(?i)\bdo\s*not\b|\bdon'?t\b|\bnever\b|\bavoid\b|\bmust\s+not\b"
    # safe once the match has to PRECEDE the command (see `forbidden_for`):
    # these read as prohibitions when they introduce it and as description when
    # they trail it.
    r"|\bcannot\b|\bcan'?t\b|\binstead\s+of\b|\brather\s+than\b"
    r"|\bwas\s+wrong\b|\bgot\s+wrong\b|\bwent\s+wrong\b"
    r"|\bprevious\s+(?:attempt|draft|output)\b|\blast\s+attempt\b"
    # `no longer` and `failed` are deliberately ABSENT: live, "Done when `ip
    # -brief link show veth105i0` reports master vmbr0 (no longer fwbr105i0)" is
    # an expected end state, and reading it as a prohibition turned a real
    # hands-on step into prose.
    r"|\bout\s+of\s+scope\s+for\b")


def forbidden_for(sentence: str, cmd: str) -> bool:
    """§17.1253 — does this sentence name THIS command in order to rule it out?

    The prohibition must come BEFORE the command. "Do NOT run `pct exec …`"
    forbids it; "Done when `ip -brief link show veth105i0` reports master vmbr0
    (no longer fwbr105i0)" describes the expected end state and happens to
    contain a negative afterwards — the first version read that as a prohibition
    and turned a real hands-on step into prose, which is the direction that
    actually loses work.

    Narrow on both axes, then: only words that genuinely frame a prohibition or a
    post-mortem, and only ahead of the command they are about.
    """
    if not sentence or not cmd:
        return False
    at = sentence.find(cmd[:40]) if len(cmd) >= 8 else sentence.find(cmd)
    if at < 0:
        at = len(sentence)
    m = _FORBIDDEN.search(sentence)
    return bool(m) and m.start() < at


def step_commands(text: str) -> list[tuple[str, str]]:
    """``[(command, sentence)]`` — every command literal in the step text
    (fenced lines and inline code / quoted runs that parse as a command) with
    the sentence it sits in."""
    t = text or ""
    out: list[tuple[str, str]] = []
    for block in _FENCE_RE.findall(t):
        for ln in block.splitlines():
            ln = re.sub(r"^\$\s+", "", ln.strip())
            if ln and not ln.startswith("#"):
                out.append((ln, ""))
    prose = _FENCE_RE.sub(" ", t)
    for sentence in re.split(r"(?<=[.!?\n])\s+", prose):
        for m in _INLINE_RE.finditer(sentence):
            cand = m.group(1).strip()
            if cand.startswith("/") or "=" in cand.split(" ", 1)[0]:
                continue                          # a path or an assignment, not a command
            out.append((cand, sentence))
        bare = _INLINE_RE.sub(" ", sentence)
        for m in _BARE_RE.finditer(bare):           # zero-width after the head: heads may be adjacent
            rest = _BARE_REST_RE.match(bare, m.end())
            if rest and _known_command(m.group(1), rest.group(1)):
                out.append((f"{m.group(1)} {rest.group(1).strip()}", sentence))
    return out


def _known_command(head: str, rest: str) -> bool:
    from app.modules.assist_state_check import _MUTATION_RE, _SUBCOMMAND_HEADS
    if head not in _SUBCOMMAND_HEADS:
        return False
    nxt = rest.split(None, 1)[0]
    if nxt.startswith("-") or nxt in _READ_VERBS:
        return True
    m = _MUTATION_RE.search(f" {head} {nxt} ")
    return bool(m) and " " in m.group(0).strip()     # the two-word form (`pct set`), not a bare `kill the …`


def _parses(cmd: str) -> bool:
    from app.modules.shell_ast import analyze
    f = analyze(cmd)
    return not f.parse_error and bool(f.commands)


def step_is_hands_on(node: dict, *, shell_backend: bool | None = None, mcp_enabled: bool | None = None) -> tuple[bool, str]:
    """``(hands_on, reason)`` for one DAG node row (``tool``, ``node_type``,
    ``title``, ``description``, ``prompt_template`` — any may be missing)."""
    from app.config import settings
    tool = str((node or {}).get("tool") or "LLM").strip().lower()
    if shell_backend is None:
        shell_backend = bool(settings.shell_tool_enabled)
    if mcp_enabled is None:
        mcp_enabled = bool(settings.mcp_tool_enabled)
    # the §17.624 tag rules, unchanged
    if tool in ("human", "human_review"):
        return True, f"tool:{tool}"
    if tool == "shell" and not shell_backend:
        return True, "tool:shell"
    if tool == "mcp" and not mcp_enabled:
        return True, "tool:mcp"
    if tool in _EXECUTABLE_TOOLS or (tool == "shell" and shell_backend) or tool == "mcp":
        return False, ""
    # what the step DOES
    text = "\n".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    cmds = step_commands(text)
    if not cmds:
        return False, ""
    for cmd, sentence in cmds:
        if forbidden_for(sentence, cmd):
            continue                              # §17.1253 — quoted to be avoided
        if command_writes(cmd):
            return True, f"writes:{cmd[:60]}"
    for cmd, sentence in cmds:
        if forbidden_for(sentence, cmd):
            continue
        if sentence and _OBSERVE_RE.search(sentence) and _parses(cmd):
            return True, f"observed:{cmd[:60]}"
    host = _HOST_RE.search(text)
    if host and any(_parses(c) for c, _ in cmds):
        return True, f"target:{host.group(0)}"
    return False, ""
