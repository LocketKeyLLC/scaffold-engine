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
#: §17.1300 — `reboot` too: live, ADD68's block ran `pct reboot 111` on a STOPPED container (a stopped
#: guest cannot reboot; `pct start` is the verb). `shutdown`/`stop` on a stopped guest are §17.1240's.
#: §17.1303 — `ssh|scp|sftp|ssh-copy-id … [user@]<ipv4>`: the literal address a block reaches.
_SSH_TARGET_RE = re.compile(r"(?<![\w./-])(?:ssh|ssh-copy-id|scp|sftp)(?![\w-])[^\n|;&]*?(?:\s|@)((?:[a-z_][a-z0-9_-]{0,31}|<[A-Z0-9_]+>)@)?((?:\d{1,3}\.){3}\d{1,3})\b")

_NEEDS_RUNNING = frozenset({"exec", "enter", "push", "pull", "reboot"})

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


#: `lvs -o lv_name,data_percent pve` → `vm-106-disk-0 0.00` (a thin LV; a thick one has no Data%)
_LVS_ROW = re.compile(r"^\s*(vm-(\d{3,5})-(?:disk|cloudinit)-\d+)\s+\S+g?\s*(\d+\.\d{2})?(?=\s|$)", re.M)


def parse_lvs(text_out: str) -> dict[str, list[dict]]:
    """``{vmid: [{name, data_percent}]}`` — §17.1288p. `data_percent` is None
    for a thick LV (nothing reported) and a float for a thin one; 0.0 means
    the volume has never been written: no partition table, no OS."""
    out: dict[str, list[dict]] = {}
    for ln in (text_out or "").split("\n"):
        m = _LVS_ROW.match(ln.rstrip())
        if not m:
            continue
        pct = m.group(3)
        out.setdefault(m.group(2), []).append({"name": m.group(1), "data_percent": float(pct) if pct else None})
    return out


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
    inv = {"cts": cts, "vms": vms, "names": parse_qm_names(qm_out)}
    # §17.1288p — and what is ON the guests' disks, cheaply: a thin volume at
    # Data% 0.00 has never been written, so the guest it belongs to has no OS.
    lvs_out = await _read(spec, "lvs --noheadings -o lv_name,lv_size,data_percent pve")
    inv["disks"] = parse_lvs(lvs_out)
    isos = await _read(spec, "ls /var/lib/vz/template/iso")
    inv["isos"] = [ln.strip() for ln in (isos or "").split("\n") if ln.strip().endswith(".iso")]
    return inv


#: §17.1288f — `VM 106`, `container 111`, `CT 120`: the guest a step is ABOUT.
_SUBJECT_RE = re.compile(r"\b(?:VM|CT|LXC|container|guest)\s*#?\s*(\d{3,5})\b", re.I)
#: a bare `ssh` (not `ssh-copy-id`, not a path)
_SSH_RE = re.compile(r"(?<![\w./-])ssh(?![\w-])")
#: §17.1299 — an unattended install with cloud-init seeds this host's key too (`--sshkeys`)
_KEY_STEP_RE = re.compile(r"ssh.*\bkey\b|public\s*key|authorized_keys|ssh-copy-id|cloud-init|cloud image|unattended", re.I)


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


_ID_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=[\"']?(\d{3,5})[\"']?\s*$", re.M)


def _resolve_ids(text_value: str) -> str:
    """§17.1288j — `VMID=106` then `qm start "$VMID"` addresses 106. Live, the
    one draft that had everything right was refused for "nothing starts it"
    because the start said `"$VMID"`. Only whole-line numeric assignments are
    resolved; the value is a guest id or it is nothing to us."""
    out = str(text_value or "")
    for m in _ID_ASSIGN_RE.finditer(out):
        name, val = m.group(1), m.group(2)
        out = re.sub(rf'"?\$\{{{re.escape(name)}\}}"?|"?\${re.escape(name)}\b"?', val, out)
    return out


def _first_line_with(texts: list[str], pattern: "re.Pattern[str]") -> str:
    for t in texts:
        for ln in str(t).split("\n"):
            if pattern.search(ln):
                return ln.strip()[:200]
    return ""


async def unmet(commands: list[str], spec, *, plan: Optional[list[dict]] = None,
                files: Optional[list[dict]] = None, node: Optional[dict] = None,
                inventory: Optional[dict] = None, truth=None) -> list[dict]:
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
    texts = [_resolve_ids(t) for t in [str(c) for c in commands or []]
             + [str((f or {}).get("content") or "") for f in files or []]]
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
        # §17.1290b — cloud-init's `--sshkeys` installs this host's key as surely as ssh-copy-id does
        copies = any("ssh-copy-id" in t or "--sshkeys" in t for t in texts)
        bare = [ln for t in texts for ln in str(t).split("\n")
                if _SSH_RE.search(ln) and "sshpass" not in ln and "ssh-copy-id" not in ln and not ln.lstrip().startswith("#")]
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
    # §17.1303 — the block reaches the step's guest at an address the machine
    # contradicts. Live, ADD84 (VM 106, measured at 192.168.1.106 by its MAC in
    # `ip neigh`, pinned as PALWORLD_IP) drew `ssh <PALWORLD_USER>@192.168.1.127`
    # -- VM 110's old address, borrowed from another step's text. A measured
    # address beats a written one; a step that names the other machine itself
    # is left alone (ADD49 legitimately reaches 192.168.1.127).
    addr = str(getattr(truth, "address", "") or "")
    tgid = str(getattr(truth, "gid", "") or "")
    if addr and tgid and tgid in subjects:
        hit = next(((t, m) for t in texts for m in _SSH_TARGET_RE.finditer(t)
                    if m.group(2) != addr and m.group(2) not in subject), None)
        if hit:
            t, m = hit
            kind = "container" if getattr(truth, "kind", "") == "ct" else "VM"
            line = t[t.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
            out.append({"command": line[:200], "why": (
                f"`{m.group(2)}` is not {kind} {tgid}'s address: the engine measured `{addr}` for its MAC "
                f"`{getattr(truth, 'mac', '') or '?'}` (`ip neigh`, read just now), and the step names no other "
                f"machine. Reach {kind} {tgid} at `{addr}` -- or, since its guest agent "
                f"{'answers' if getattr(truth, 'agent', False) else 'may answer'}, through `qm guest exec {tgid}` "
                f"with no address at all.")})
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
    # §17.1288p — the step reaches INTO a VM whose only disks have never been
    # written: there is no OS to log into. Live, ADD82 ran twice against VM 106
    # (`qm list` running, `bridge fdb` never saw its MAC, scsi0 read 222 KB in
    # 30 min) before anyone read `lvs`: vm-106-disk-0 100G Data% 0.00, blkid
    # empty. ADD5 "Install Ubuntu Server" was recorded done, and ADD53 later
    # removed that disk (`pvesm free`) and created an empty one. The engine
    # had the read all along.
    disks = inv.get("disks") or {}
    # §17.1290b — the step that INSTALLS the OS is the one step the empty disk
    # does not block: live, ADD117 (cloud image + cloud-init) was refused for
    # the very condition it exists to end, because its script sshes in after.
    installs_os = bool(re.search(r"\binstall\b.*\b(?:ubuntu|debian|operating system|os\b|server \d\d\.\d\d)", subject, re.I)) \
        or any(re.search(r"\bqm\s+importdisk\b|\bqm\s+disk\s+import\b|local-lvm:cloudinit|--cicustom\b", t) for t in texts)
    for gid in subjects:
        if installs_os:
            break
        if gid not in vms or not (uses_ssh or any(re.search(rf"\bqm\s+guest\s+exec\s+{gid}\b", t) for t in texts)):
            continue
        mine = [d for d in disks.get(gid, []) if "cloudinit" not in d["name"]]
        if not mine or any(d["data_percent"] is None or d["data_percent"] > 0.0 for d in mine):
            continue
        names = ", ".join(d["name"] for d in mine)
        installed = next((f"{n.get('node_key')} · {str(n.get('title'))[:50]}" for n in plan or []
                          if (n.get("status") or "") == "done"
                          and re.search(r"\binstall\b.*\b(?:ubuntu|debian|os|server)\b", str(n.get("title") or ""), re.I)
                          and (gid in str(n.get("title") or "") or _norm((inv.get("names") or {}).get(gid, "")) in _norm(str(n.get("title") or "")))), "")
        isos = ", ".join((inv.get("isos") or [])[:3]) or "none found in /var/lib/vz/template/iso"
        out.append({"command": _first_line_with(texts, _SSH_RE) or f"ssh into {gid}", "why": (
            f"VM {gid}'s disk {names} has never been written (`lvs` Data% 0.00; no partition table, no "
            f"filesystem): there is no OS to log into, so an ssh or a guest agent cannot exist there yet. "
            + (f"{installed} is recorded done, but a later step recreated the disk, so that install is gone. "
               if installed else "")
            + f"Ubuntu must be installed on VM {gid} before this step (ISOs on the host: {isos}) -- an OS "
            f"install is a console step, or an autoinstall from the ISO; it is not this step.")})
        break
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
                kind = "container" if is_ct else "VM"
                if verb == "reboot":
                    tail = f" A stopped guest cannot reboot: write `{tool} start {gid}` instead, which also applies the new config."
                elif fix:
                    tail = f" {fix} is the step that starts it, and it has not run yet."
                else:
                    tail = f" Start it first (`{tool} start {gid}`)."
                out.append({"command": cmd, "why": f"{kind} {gid} is {status}, so `{tool} {verb}` fails before it starts." + tail})
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
    action = re.compile(rf"\b{re.escape(tool)}\s+{re.escape(verb)}\s+{re.escape(gid)}\b")
    # §17.1294 — in a FILE, `if qm status N | grep -q running; then … else qm start N; fi` is the same
    # guard in the shell's other spelling: a status read of the SAME guest earlier in the script
    # counts. Live, the engine's own watch_guest_boot template was refused for its `else qm start`.
    if "\n" in text_value:
        m = action.search(text_value)
        if m and re.search(rf"\b{re.escape(tool)}\s+status\s+{re.escape(gid)}\b", text_value[:m.start()]):
            return True
    if "||" not in text_value:
        return False
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
