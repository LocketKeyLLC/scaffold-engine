"""§17.1213 — do not run a block the machine already contradicts, and say what
would fix it.

Two steps ran on the operator's host while the engine had a read-only channel to
it, and both were doomed before they were sent:

    ADD21  pct exec 111 -- pm2 start 1   ->  exited 255: container '111' not running!
    ADD82  pct exec 106 -- apt-get …     ->  106 is a VM, not a container

The first had a prerequisite the DAG never expressed (ADD50 "Start container
111" is still pending). The second is simply the wrong tool for that guest: `pct`
addresses containers, 106 is a VM, and one `qm list` says so.

The engine could have known both. It had the channel, it had been reading that
host all evening, and it ran them anyway — then reported the failures as though
the machine had surprised it.

So: before a hands-on block is offered, the guests it names are checked against
what the host actually reports. `pct list` and `qm list`, read-only, once each.
A contradiction is not a decision for anyone — it goes into the frame's
`refused`, which turns Run off and flips the suggestion to "I'll do it myself"
— and the refusal NAMES the remedy, including the plan step that would satisfy
it when one exists. The operator asked for exactly that: *"if something needs
the user's input, the engine should request it from the user."* A greyed button
is not a request.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("scaffold")

#: `pct exec 111 -- …`, `qm set 106 --scsi0 …`. The verb may be absent
#: (`qm 106` is not valid, so a verb is required).
_GUEST_RE = re.compile(r"\b(pct|qm)\s+([a-z][a-z-]*)\s+(\d{3,5})\b")

#: `pct list` → `VMID Status Lock Name`
_PCT_ROW = re.compile(r"^\s*(\d{3,5})\s+(\S+)", re.M)
#: `qm list` → `VMID NAME STATUS …`
_QM_ROW = re.compile(r"^\s*(\d{3,5})\s+(\S+)\s+(\S+)", re.M)

#: Verbs that need the guest actually RUNNING, not merely defined.
_NEEDS_RUNNING = frozenset({"exec", "enter", "push", "pull"})

#: §17.1240 — verbs whose job is ALREADY DONE, which is not the same as working.
#: `pct start` on a running container exits non-zero ("already running"), so a
#: retry of a start that succeeded fails, and a step that is genuinely finished
#: gets recorded as broken. Live: ADD50's `pct start 111` worked, its response
#: was lost, and every retry after that could only fail because 111 was up. The
#: remedy is the guard chain CHANNEL_RULES already asks for.
_ALREADY = {"start": "running", "stop": "stopped", "shutdown": "stopped"}

#: §17.1243 — verbs that BRING A GUEST INTO EXISTENCE. For these, "there is no
#: guest N on this host" is not a blocker, it is the reason the command is being
#: run. Live, ADD111 (set up Pi-hole): `pct create 130 …` was refused with "there
#: is no guest 130 on this host — `pct list` and `qm list` do not have it", and
#: `pct start 130` and `pct exec 130 -- …` later in the SAME block were refused
#: for the same reason, so a correct 11-command runbook was unrunnable and the
#: frame fell back to "I'll do it myself". A create is refused only for the
#: opposite reason: the id is already taken.
_CREATES = frozenset({"create", "restore", "clone"})


def guests_in(commands: list[str]) -> list[tuple[str, str, str]]:
    """``[(tool, verb, id)]`` the commands address, in order, deduplicated."""
    out: list[tuple[str, str, str]] = []
    for c in commands or []:
        for m in _GUEST_RE.finditer(str(c)):
            t = (m.group(1), m.group(2), m.group(3))
            if t not in out:
                out.append(t)
    return out


def parse_pct_list(text_out: str) -> dict[str, str]:
    """``{ctid: status}``. The header row is skipped by the digit anchor."""
    return {m.group(1): m.group(2).lower() for m in _PCT_ROW.finditer(text_out or "")}


def parse_qm_list(text_out: str) -> dict[str, str]:
    """``{vmid: status}`` — `qm list` puts NAME between the id and the status."""
    return {m.group(1): m.group(3).lower() for m in _QM_ROW.finditer(text_out or "")}


async def unmet(commands: list[str], spec, *, plan: Optional[list[dict]] = None) -> list[dict]:
    """``[{command, why}]`` for every command the host contradicts.

    `plan` is the job's nodes, so a refusal can name the step that would make
    this one runnable rather than leaving the operator to find it. Fail-soft:
    if the host cannot be read, nothing is refused — this must never invent a
    blocker out of its own blindness.
    """
    guests = guests_in(commands)
    if not guests or spec is None:
        return []
    cts = parse_pct_list(await _read(spec, "pct list"))
    vms = parse_qm_list(await _read(spec, "qm list"))
    if not cts and not vms:
        logger.warning("preconditions_unreadable — nothing refused")
        return []

    out: list[dict] = []
    # §17.1243 — guests an earlier command in this same block brings into being.
    # `guests_in` preserves command order, so a create is always seen before the
    # start and the exec that follow it.
    made: set[str] = set()
    for tool, verb, gid in guests:
        cmd = next((c for c in commands if re.search(rf"\b{tool}\s+{verb}\s+{gid}\b", str(c))), f"{tool} {verb} {gid}")
        is_ct, is_vm = gid in cts, gid in vms
        if verb in _CREATES:
            if is_ct or is_vm:
                out.append({"command": cmd, "why": (
                    f"id {gid} is already taken on this host — it is "
                    f"{'a container' if is_ct else 'a VM'} — so `{tool} {verb} {gid}` would collide "
                    f"with it. Proxmox shares one id space between containers and VMs, so pick an id "
                    f"in neither `pct list` nor `qm list`. If this step ALREADY created {gid} and is "
                    f"being run again to finish what comes after, guard the create instead of "
                    f"repeating it: `{tool} status {gid} >/dev/null 2>&1 || {tool} {verb} {gid} …` — "
                    f"then the rest of the block can run without building it twice.")})
            else:
                made.add(gid)
            continue
        if gid in made:
            continue                  # created earlier in this very block
        if tool == "pct" and is_vm and not is_ct:
            out.append({"command": cmd, "why": (
                f"{gid} is a VM on this host, not a container — `pct` cannot address it. "
                f"The same thing for a VM is `qm {verb}` (or `qm guest exec` inside it).")})
        elif tool == "qm" and is_ct and not is_vm:
            out.append({"command": cmd, "why": (
                f"{gid} is a container on this host, not a VM — `qm` cannot address it. Use `pct {verb}`.")})
        elif not is_ct and not is_vm:
            out.append({"command": cmd, "why": f"there is no guest {gid} on this host — `pct list` and `qm list` do not have it."})
        elif verb in _NEEDS_RUNNING:
            status = cts.get(gid) if is_ct else vms.get(gid)
            if status and status != "running":
                fix = _step_that_starts(gid, plan)
                out.append({"command": cmd, "why": (
                    f"{'container' if is_ct else 'VM'} {gid} is {status}, so `{tool} {verb}` fails before it starts."
                    + (f" {fix} is the step that starts it, and it has not run yet." if fix
                       else f" Start it first (`{tool} start {gid}`)."))})
        elif verb in _ALREADY:
            status = cts.get(gid) if is_ct else vms.get(gid)
            if status and status == _ALREADY[verb] and not _guarded(cmd, tool, verb, gid):
                kind = "container" if is_ct else "VM"
                out.append({"command": cmd, "why": (
                    f"{kind} {gid} is ALREADY {status}, and `{tool} {verb}` on it exits non-zero — so "
                    f"this step would be recorded as broken for work that is already done. Make it "
                    f"idempotent instead: `{tool} status {gid} | grep -q {status} || {tool} {verb} {gid}`, "
                    f"where the check and the action are each a whole command.")})
    if out:
        logger.warning("preconditions_unmet count=%d first=%r", len(out), out[0]["why"][:120])
    return out


def _guarded(cmd: str, tool: str, verb: str, gid: str) -> bool:
    """§17.1249 — is this action already behind a check on the same guest?

    `pct status 130 | grep -q running || pct start 130` is the idempotent form
    §17.1240's OWN refusal asks for, and §17.1240 refused it: `guests_in` sees a
    `start` on a running container and never notices the `||` in front of it. A
    gate that rejects the remedy it recommends is worse than no gate — live, it
    was the single refusal standing between ADD111 and a correct block.

    Guarded means: the action sits after a `||`, and something before it reads
    the same guest. That is the whole shape — a check that fails hands over to
    the fix, and a check that passes skips it.
    """
    text_value = str(cmd or "")
    if "||" not in text_value:
        return False
    action = re.compile(rf"\b{re.escape(tool)}\s+{re.escape(verb)}\s+{re.escape(gid)}\b")
    parts = text_value.split("||")
    for i, part in enumerate(parts):
        if i == 0 or not action.search(part):
            continue
        before = "||".join(parts[:i])
        if re.search(rf"\b{re.escape(gid)}\b", before):
            return True
    return False


def _step_that_starts(gid: str, plan: Optional[list[dict]]) -> Optional[str]:
    """The pending plan step whose title says it starts this guest — so the
    refusal points at the fix instead of describing the problem twice."""
    for n in plan or []:
        if (n.get("status") or "") not in ("pending", "failed"):
            continue
        title = str(n.get("title") or "")
        if gid in title and re.search(r"\bstart\b", title, re.I):
            return f"{n.get('node_key')} · {title[:60]}"
    return None


async def _read(spec, command: str) -> str:
    """One read-only listing. Never raises; a refusal is not an answer."""
    try:
        from app.modules.assist_local_runner import _plain_output, not_evidence
        from app.modules.mcp_client import call_tool
        res = await call_tool(spec, "run_readonly", {"command": command, "timeout_s": 15})
        out = _plain_output(res)
        return "" if not_evidence(out, bool(res.is_error)) else out
    except Exception as exc:
        logger.warning("precondition_read_failed cmd=%r err=%r", command, exc)
        return ""
