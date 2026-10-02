"""§17.1215 — a drafted block must do what its step said it would do.

The gate checks the SHAPE of a command (§17.1187). §17.1213 checks the machine
can carry it. Nothing checked the block against the step's own intent, and that
is the gap that nearly left a server down:

ADD94 spells its work out, in its own description, in a fenced block:

    qm stop 106
    qm set 106 --scsi0 local-lvm:vm-106-disk-0
    qm disk resize 106 scsi0 100G
    qm set 106 --boot order=scsi0
    qm start 106

The engine drafted:

    qm config 106
    qm rescan --vmid 106
    qm set 106 --scsi0 <DISK_NAME>
    qm resize 106 scsi0 100G

No stop, no boot order, no start. On a RUNNING VM whose `boot: order=ide2`
points at an empty CD-ROM, that block runs clean, reports success, and leaves
`palworld-server` exactly as down as it was — the disk attached to a guest that
never rebooted onto it. Every existing check passes it: the shapes are legal,
the host can run them, the verify lines even look plausible.

So: when a step names its commands, the draft is compared against them and the
omissions are quoted back for one redraft — the same move §17.1196 makes for a
gate refusal, which is the mechanism that produced the correct block when this
was done by hand.

Deterministic, and deliberately one-directional: a draft may add commands (a
`qm status` between steps is fine), it may not drop the ones the step named.
Comparison is on a SIGNATURE — tool, verb, numeric target, first flag — so
`qm set 106 --boot order=scsi0` and `qm set 106 --scsi0 …` are not confused for
each other, which a head-only match would do.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("scaffold")

_FENCE_RE = re.compile(r"```(?:bash|sh|shell)?\s*\n(.*?)```", re.S)
_NUM_RE = re.compile(r"^\d{1,6}$")
#: Placeholders stand in for values, so they must not make two commands differ.
_PH_RE = re.compile(r"<[A-Z][A-Z0-9_]{1,40}>")


def commands_in(text: str) -> list[str]:
    """The commands a step's own description spells out, in order."""
    out: list[str] = []
    for m in _FENCE_RE.finditer(text or ""):
        for raw in m.group(1).splitlines():
            ln = raw.strip()
            if not ln or ln.startswith("#") or ln.startswith("$ ") and not ln[2:].strip():
                continue
            ln = re.sub(r"^\$\s+", "", ln)
            if ln and ln not in out:
                out.append(ln)
    return out


def signature(cmd: str) -> str:
    """What makes two commands the same INSTRUCTION: the tool, the verb, the
    numeric target, and the first long flag. Values are dropped — a step naming
    `--scsi0 local-lvm:…` is satisfied by a draft with `--scsi0 <DISK_NAME>`.
    """
    toks = _PH_RE.sub("X", cmd or "").split()
    if not toks:
        return ""
    parts = [toks[0]]
    if len(toks) > 1 and not toks[1].startswith("-"):
        parts.append(toks[1])
    for t in toks[1:]:
        if _NUM_RE.match(t):
            parts.append(t)
            break
    for t in toks[1:]:
        if t.startswith("--"):
            parts.append(t.split("=", 1)[0])
            break
    return " ".join(parts)


#: Two-word subcommands Proxmox accepts both ways. Folded BEFORE the signature
#: is taken, because the signature keeps only one verb token — folding it
#: afterwards loses the verb entirely and every comparison silently misses.
_ALIASES = (
    (re.compile(r"^\s*qm\s+disk\s+(resize|move|import|rescan|unlink)\b"), r"qm \1"),
    (re.compile(r"^\s*pct\s+disk\s+(resize|move)\b"), r"pct \1"),
)


def _canon(cmd: str) -> str:
    out = (cmd or "").strip()
    for rx, rep in _ALIASES:
        out = rx.sub(rep, out)
    return out


_QUOTED = re.compile(r"""(['"])((?:(?!\1).)+)\1""")


def _expand(drafted: list[str], files: Optional[list[dict]] = None) -> list[str]:
    """§17.1287 — every place a step's command can be carried out: a command of
    its own; a segment of a compound line; the payload inside an ``ssh host
    '…'`` / ``pct exec N -- …`` / ``bash -c '…'`` quote; a line of a written
    file (and ITS quoted payloads). Live, ADD82's clean redraft ran
    `sudo apt-get install -y qemu-guest-agent` INSIDE `ssh user@$IP "…"` inside
    a script -- and this pass, reading only top-level commands, called it
    missing, redrafted, and accepted the refused host-side block because it
    "covered" the words."""
    out: list[str] = []
    seeds = [str(c or "") for c in (drafted or [])]
    for f in files or []:
        seeds += str((f or {}).get("content") or "").splitlines()
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        t = stack.pop().strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        for m in _QUOTED.finditer(t):
            stack.append(m.group(2))
        for part in re.split(r"\s*(?:&&|\|\||;)\s*", t):
            if part.strip() and part.strip() != t:
                stack.append(part)
    return out


def uncovered(step_text: str, drafted: list[str], files: Optional[list[dict]] = None) -> list[str]:
    """Commands the step named that the draft does not carry out.

    Empty when the step names none — most steps describe intent in prose, and
    this must never invent a requirement out of a step that stated none.
    """
    want = commands_in(step_text)
    if not want:
        return []
    have = {signature(_canon(c)) for c in _expand(drafted, files)}
    missing = [c for c in want if signature(_canon(c)) not in have]
    if missing:
        logger.warning("runbook_coverage_gap missing=%d first=%r", len(missing), missing[0][:70])
    return missing


def coverage_retry_note(missing: list[str], state: Optional[str] = None) -> str:
    """The note that goes back into a second draft — §17.1196's move, pointed at
    intent instead of shape. Quotes what was dropped, in the step's own words."""
    if not missing:
        return ""
    lines = "\n".join(f"    {c}" for c in missing)
    note = ("THIS DRAFT DOES NOT DO WHAT THE STEP SAID.\n\n"
            "The step spells out commands your block leaves out entirely:\n\n"
            f"{lines}\n\n"
            "A block that skips them can still run clean and report success while the "
            "step's own 'Done when' is false — which is worse than failing. Redraft so "
            "every command above is carried out, in an order that makes sense, keeping "
            "whatever else your draft needs.")
    if state:
        note += f"\n\n{state}"
    return note
