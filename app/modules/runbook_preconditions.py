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


def parse_qm_names(text_out: str) -> dict[str, str]:
    """``{vmid: name}`` from the same listing (§17.1288f — a plan step names a
    VM by its name as often as by its id: "the AI VM", `ai-vm`)."""
    return {m.group(1): m.group(2) for m in _QM_ROW.finditer(text_out or "")}


async def read_inventory(spec) -> Optional[dict]:
    """§17.1288f — the two listings, read ONCE per pause and handed to every
    draft's `unmet`. ``None`` when the host cannot be read (then nothing is
    refused: a blocker is never invented out of blindness)."""
    if spec is None:
        return None
    pct_out, qm_out = await _read(spec, "pct list"), await _read(spec, "qm list")
    cts, vms = parse_pct_list(pct_out), parse_qm_list(qm_out)
    if not cts and not vms:
        logger.warning("preconditions_unreadable — nothing refused")
        return None
    return {"cts": cts, "vms": vms, "names": parse_qm_names(qm_out)}


#: §17.1288f — `VM 106`, `container 111`, `CT 120`: the guest a step is ABOUT.
_SUBJECT_RE = re.compile(r"\b(?:VM|CT|LXC|container|guest)\s*#?\s*(\d{3,5})\b", re.I)
#: a bare `ssh` (not `ssh-copy-id`, not a path)
_SSH_RE = re.compile(r"(?<![\w./-])ssh(?![\w-])")
_KEY_STEP_RE = re.compile(r"ssh.*\bkey\b|public\s*key|authorized_keys|ssh-copy-id", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def key_known_for(gid: str, name: str, plan: Optional[list[dict]]) -> Optional[str]:
    """§17.1288g — the FINISHED plan step that put this host's key on guest
    `gid` (named by id or by name), or None. Live: ADD26 "Install the SSH
    public key on the AI VM (192.168.1.129)" is done, for VM 110 `ai-vm`;
    nothing of the kind exists for VM 106."""
    for n in plan or []:
        if (n.get("status") or "") != "done":
            continue
        title = str(n.get("title") or "")
        if not _KEY_STEP_RE.search(title):
            continue
        if re.search(rf"\b{re.escape(gid)}\b", title) or (name and _norm(name) and _norm(name) in _norm(title)):
            return f"{n.get('node_key')} · {title[:60]}"
    return None


def _first_line_with(texts: list[str], pattern: "re.Pattern[str]") -> str:
    for t in texts:
        for ln in str(t).split("\n"):
            if pattern.search(ln):
                return ln.strip()[:200]
    return ""


async def unmet(commands: list[str], spec, *, plan: Optional[list[dict]] = None,
                files: Optional[list[dict]] = None, node: Optional[dict] = None,
                inventory: Optional[dict] = None) -> list[dict]:
    """``[{command, why}]`` for every command the host contradicts.

    `plan` is the job's nodes, so a refusal can name the step that would make
    this one runnable rather than leaving the operator to find it. Fail-soft:
    if the host cannot be read, nothing is refused — this must never invent a
    blocker out of its own blindness.

    §17.1288f — the FILES a block writes are read too (the live ADD82 script
    addressed VM 106 only from inside `/tmp/install_agent_106.sh`), and the
    guest the STEP is about is checked even when no command names it: a
    stopped guest that nothing in the block starts, and an ssh into a guest
    that holds no key of this host's, are both contradictions the engine can
    see before sending anything.
    """
    texts = [str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []]
    guests = guests_in(texts)
    subject = str((node or {}).get("title") or "") + "\n" + str((node or {}).get("description") or "")
    subjects = list(dict.fromkeys(_SUBJECT_RE.findall(subject))) if node else []
    uses_ssh = any(_SSH_RE.search(t) for t in texts)
    out: list[dict] = []
    inv = inventory if inventory is not None else (await read_inventory(spec) if spec is not None else None)
    # §17.1288g — an ssh into the step's guest with no key of ours on it and
    # none copied in this block. Judged from the plan (the engine's own record
    # of what it finished), so it needs no host read.
    if uses_ssh and subjects:
        copies = any("ssh-copy-id" in t for t in texts)
        bare = [ln for t in texts for ln in str(t).split("\n")
                if _SSH_RE.search(ln) and "sshpass" not in ln and "ssh-copy-id" not in ln]
        if bare and not copies:
            names = (inv or {}).get("names") or {}
            for gid in subjects:
                known = key_known_for(gid, names.get(gid, ""), plan)
                if known:
                    continue
                out.append({"command": bare[0].strip()[:200], "why": (
                    f"nothing has put this host's key on guest {gid}: no finished step installed one there "
                    f"and this block copies none, so `ssh -o BatchMode=yes` is refused by the guest before "
                    f"anything runs (publickey). Before the first ssh: `SSHPASS=\"$MASS_PASSWORD\" sshpass -e "
                    f"ssh-copy-id -o StrictHostKeyChecking=accept-new \"$USER@$IP\"` -- or prefix the ssh "
                    f"itself with `SSHPASS=\"$MASS_PASSWORD\" sshpass -e`. The password travels by name; "
                    f"nothing here can type one.")})
                break
    if (not guests and not subjects) or not inv:
        return out
    cts, vms = inv["cts"], inv["vms"]
    # §17.1288f — the step is about a stopped guest, the block needs it up
    # (an ssh, or a wait for `running`) and nothing in the block starts it.
    # Live: the script waited 60 s for VM 106 to be running, then swept and
    # read `ip neigh`, then sshed -- and `qm list` had said `stopped` all along.
    for gid in subjects:
        status = cts.get(gid) if gid in cts else vms.get(gid)
        if status != "stopped":
            continue
        starts = any(re.search(rf"\b(?:pct|qm)\s+start\s+{gid}\b", t) for t in texts)
        waits = any(re.search(rf"\b(?:pct|qm)\s+status\s+{gid}\b.*running", t) for t in texts)
        if starts or not (uses_ssh or waits):
            continue
        tool = "pct" if gid in cts else "qm"
        fix = _step_that_starts(gid, plan)
        out.append({"command": _first_line_with(texts, re.compile(rf"\b{gid}\b|(?<![\w./-])ssh(?![\w-])")) or f"ssh into {gid}", "why": (
            f"{'container' if tool == 'pct' else 'VM'} {gid} is stopped (`{tool} list`, read just now) and nothing "
            f"in this block starts it -- it {'waits for it to be running and then ' if waits else ''}reaches into "
            f"it, which cannot happen. Start it first, guarded: `{tool} status {gid} | grep -q running || "
            f"{tool} start {gid}`, then wait for it with a loop of reads, and only then find its address "
            f"(sweep the bridge's /24 and read `ip neigh` for its MAC, up to 12 × 5 s) and ssh."
            + (f" {fix} is the plan step that starts it, and it has not run yet." if fix else ""))})
    if not guests:
        if out:
            logger.warning("preconditions_unmet count=%d first=%r", len(out), out[0]["why"][:120])
        return out
    commands = texts
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
