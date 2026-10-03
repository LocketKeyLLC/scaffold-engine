"""§17.1289 — machine truth before every hands-on step.

The day of 2026-10-02 produced fourteen engine fixes from one step, and the
set of them says one thing: the engine trusted its own record over the machine.
ADD5 "Install Ubuntu" stayed `done` for two weeks after ADD53 had run
`pvesm free` on that disk; the ADD82 description said "the host has NO way in"
for eleven reasks; `qm list` saying `running` was read as "booted". Every
precondition written that day (§17.1213, §17.1288f/g/l/p) is one instance of a
rule the engine did not have in general:

    before drafting a hands-on step, re-measure the guest the step is about
    and reconcile the plan, the step text and the facts against what it says.

This module is that rule. ``read_guest_truth`` measures one guest with reads
the runner already allows; ``step_needs`` says what the step requires of it;
``contradictions`` is ONE table of judgments (the finished plan step whose
effect the machine does not show, the stale claim in the step's own text);
``reconcile_from_truth`` acts on them through the existing primitives only —
a fact in the ledger, a reopened step (no cascade), a corrected text — and
says what it did. Fail-soft everywhere: an unreadable field is ``None`` and
``None`` contradicts nothing; a blocker is never invented out of blindness.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("scaffold")

_SUBJECT_RE = re.compile(r"\b(?:VM|CT|LXC|container|guest)\s*#?\s*(\d{3,5})\b", re.I)
_SSH_RE = re.compile(r"(?<![\w./-])ssh(?![\w-])")
_NET0_LINE_RE = re.compile(r"^net0:\s*(.+)$", re.M)
_MAC_RE = re.compile(r"\b(?:virtio|e1000e?|vmxnet3|rtl8139|hwaddr|macaddr)=([0-9A-Fa-f:]{17})")
_BRIDGE_RE = re.compile(r"\bbridge=(\w+)")
_HOSTNAME_RE = re.compile(r"^hostname:\s*(\S+)", re.M)


def parse_net0(config: str) -> tuple[Optional[str], Optional[str]]:
    """``(mac, bridge)`` from a ``qm config`` OR ``pct config`` net0 line. §17.1301 —
    a VM writes the MAC first (`net0: virtio=BC:…,bridge=vmbr0`); a container
    writes it fourth (`net0: name=eth0,bridge=vmbr0,firewall=1,hwaddr=BC:…`), and
    the regex that wanted it first measured every container as MAC-less."""
    m = _NET0_LINE_RE.search(config or "")
    if not m:
        return None, None
    line = m.group(1)
    mac = _MAC_RE.search(line)
    br = _BRIDGE_RE.search(line)
    return (mac.group(1).lower() if mac else None), (br.group(1) if br else None)
_AGENT_CFG_RE = re.compile(r"^agent:\s*(\d)", re.M)
_INSTALL_OS_RE = re.compile(r"\binstall\b.*\b(?:ubuntu|debian|os|operating system|server \d\d\.\d\d)\b", re.I)
#: §17.1313 — commands that reach the internet by NAME from inside the guest
_NET_FETCH_RE = re.compile(r"(?<![\w-])(?:apt-get|apt|aptitude|dnf|yum|zypper|apk|pip3?|npm|pnpm|yarn|gem|cargo|go\s+install|curl|wget|git\s+clone|snap|add-apt-repository|pveam\s+download)(?![\w-])")

#: §17.1327 — a unit as `systemctl list-unit-files` prints it, and its suffix.
_UNIT_NAME_RE = re.compile(r"[A-Za-z0-9@:._-]+\.(?:service|socket|timer|target)")
_UNIT_SUFFIX_RE = re.compile(r"\.(?:service|socket|timer|target)$")

_START_RE = re.compile(r"\bstart\b.*\b(?:VM|container|CT)\b", re.I)
_SSH_KEY_RE = re.compile(r"ssh.*\bkey\b|public\s*key|authorized_keys|ssh-copy-id", re.I)
FACT_PREFIX = "ENGINE MEASURED"


@dataclass
class GuestTruth:
    gid: str
    kind: Optional[str] = None            # "vm" | "ct" | None (not on this host, or unreadable)
    name: str = ""
    status: Optional[str] = None          # "running" | "stopped" | None
    disks: list[dict] = field(default_factory=list)   # [{name, data_percent}]
    has_os: Optional[bool] = None         # False: every thin disk at 0.00; True: some written; None: unknown
    mac: Optional[str] = None
    bridge: Optional[str] = None
    transmits: Optional[bool] = None      # the bridge has learned the MAC
    address: Optional[str] = None         # the neigh entry for the MAC
    agent: Optional[bool] = None          # qm agent N ping answered
    key_known_by: Optional[str] = None    # the finished plan step that installed this host's key
    resolves: Optional[bool] = None       # §17.1313 — `getent hosts deb.debian.org` inside the guest answered
    units: Optional[list[str]] = None     # §17.1327 — the service units the guest HAS (`systemctl list-unit-files`)
    dns_hint: str = ""                    # §17.1313 — a sibling guest's `nameserver:` line, for the fix step
    reads: dict[str, str] = field(default_factory=dict)   # evidence, by read

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "reads"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _mentions_guest(title: str, gid: str, name: str) -> bool:
    if re.search(rf"\b{re.escape(gid)}\b", title):
        return True
    if _norm(name) and _norm(name) in _norm(title):
        return True
    # §17.1315 — "Configure PalWorld service" names guest `palworld-server` by its first word;
    # a distinctive token (5+ chars, not a generic noun) counts.
    head = re.split(r"[-_. ]", str(name or ""), 1)[0]
    return len(head) >= 5 and head.lower() not in _GENERIC_NAME_WORDS and bool(re.search(rf"\b{re.escape(head)}\b", title, re.I))


_GENERIC_NAME_WORDS = frozenset({"server", "service", "guest", "container", "ubuntu", "debian", "proxy", "panel", "control", "media", "download", "client"})


async def _probe(spec, command: str) -> tuple[Optional[bool], str]:
    """``(ok, output)`` — ``ok`` None when the runner could not be asked."""
    try:
        from app.modules.assist_local_runner import _plain_output
        from app.modules.mcp_client import call_tool
        res = await call_tool(spec, "run_readonly", {"command": command, "timeout_s": 15})
        return (not bool(res.is_error)), (_plain_output(res) or "")
    except Exception as exc:
        logger.warning("machine_truth_probe_failed cmd=%r err=%r", command, exc)
        return None, ""


def truth_from_texts(gid: str, *, inventory: Optional[dict], qm_config: str = "", neigh: str = "",
                     fdb: str = "", agent_ping: Optional[tuple[Optional[bool], str]] = None,
                     plan: Optional[list[dict]] = None, dns: Optional[tuple[Optional[bool], str]] = None,
                     sibling_config: str = "", units_text: str = "") -> GuestTruth:
    """The pure half: build the truth from texts the reads returned. Tested on
    the live fixtures; ``read_guest_truth`` only fetches the texts."""
    from app.modules.runbook_preconditions import key_known_for
    inv = inventory or {}
    t = GuestTruth(gid=gid)
    cts, vms, names = inv.get("cts") or {}, inv.get("vms") or {}, inv.get("names") or {}
    if gid in cts:
        t.kind, t.status = "ct", cts[gid]
    elif gid in vms:
        t.kind, t.status, t.name = "vm", vms[gid], str(names.get(gid) or "")
    elif inv:
        t.kind = None
    disks = [d for d in (inv.get("disks") or {}).get(gid, []) if "cloudinit" not in str(d.get("name") or "")]
    t.disks = disks
    if disks:
        pcts = [d.get("data_percent") for d in disks]
        if all(p is not None for p in pcts):
            t.has_os = any(float(p) > 0.0 for p in pcts)
        elif any(p is not None and float(p) > 0.0 for p in pcts):
            t.has_os = True
    if qm_config:
        t.mac, t.bridge = parse_net0(qm_config)
        if t.kind == "ct" and not t.name:
            hm = _HOSTNAME_RE.search(qm_config)
            t.name = hm.group(1) if hm else ""
        t.reads[("pct" if t.kind == "ct" else "qm") + " config"] = qm_config[:600]
    if t.mac and neigh:
        hit = next((ln for ln in neigh.split("\n") if t.mac in ln.lower()), "")
        t.address = hit.split()[0] if hit else None
        t.reads["ip neigh"] = hit or "(no entry for the MAC)"
    if t.mac and fdb is not None and fdb != "":
        t.transmits = t.mac in fdb.lower()
        t.reads["bridge fdb"] = "learned" if t.transmits else "(never seen)"
    elif t.mac and fdb == "":
        t.transmits = None
    if agent_ping is not None:
        ok, out = agent_ping
        if ok is not None:
            low = (out or "").lower()
            # live: "QEMU guest agent is not running" (106), "No QEMU guest agent configured" (110)
            t.agent = bool(ok) and not any(w in low for w in ("not running", "not configured", "no qemu", "error", "timeout"))
            t.reads["qm agent ping"] = (out or ("answered" if t.agent else "no answer"))[:200]
    # §17.1313 — does the guest resolve names? Live, CT 120's /etc/resolv.conf was the
    # host's Tailscale stub (100.100.100.100); `apt-get update` inside it hung for the
    # runner's full 180 s, twice, and the step failed twice. One read settles it.
    if dns is not None:
        ok, out = dns
        if ok is not None:
            t.resolves = bool(ok) and bool((out or "").strip()) and "not running" not in (out or "").lower()
            t.reads["getent hosts"] = ((out or "").strip() or "(printed nothing)")[:200]
    if sibling_config:
        m = re.search(r"^nameserver:\s*(.+)$", sibling_config, re.M)
        if m:
            t.dns_hint = " ".join(m.group(1).split())
    # §17.1327 — the units the guest HAS, so a block that acts on one is judged against the
    # machine and not against whatever happens to be in view. Live, a model draft ran
    # `systemctl is-enabled palworld-server` (the VM's hostname) while the machine held
    # `palworld.service`; and a legitimate `systemctl restart control-panel` would have been
    # refused for a unit CT 111 really has, because no step in view mentioned it.
    if units_text:
        names = []
        for ln in units_text.split("\n"):
            w = ln.split()
            if w and _UNIT_NAME_RE.fullmatch(w[0]):
                names.append(_UNIT_SUFFIX_RE.sub("", w[0]))
        t.units = names[:400]
        t.reads["systemctl list-unit-files"] = f"{len(t.units)} units"
    t.key_known_by = key_known_for(gid, t.name, plan)
    return t


async def read_guest_truth(spec, gid: str, inventory: Optional[dict], plan: Optional[list[dict]] = None) -> GuestTruth:
    """Measure guest ``gid`` once: the inventory (already read this pause) plus
    four reads. Every read is fail-soft; a field that could not be read is None."""
    qm_config = neigh = fdb = ""
    agent_ping: Optional[tuple[Optional[bool], str]] = None
    kind = "ct" if gid in ((inventory or {}).get("cts") or {}) else ("vm" if gid in ((inventory or {}).get("vms") or {}) else None)
    # §17.1301 — a container has a config too (`pct config`): live, CT 111 was
    # measured with no MAC, no bridge and no presence because only VMs were read.
    if spec is not None and kind in ("vm", "ct"):
        _ok, qm_config = await _probe(spec, f"{'qm' if kind == 'vm' else 'pct'} config {gid}")
        if parse_net0(qm_config or "")[0]:
            _ok, neigh = await _probe(spec, "ip neigh show")
            _ok, fdb_out = await _probe(spec, "bridge fdb show")
            fdb = fdb_out if _ok else ""
        if kind == "vm" and ((inventory or {}).get("vms") or {}).get(gid) == "running":
            agent_ping = await _probe(spec, f"qm agent {gid} ping")
    dns: Optional[tuple[Optional[bool], str]] = None
    sibling_config = ""
    cts = (inventory or {}).get("cts") or {}
    if spec is not None and kind == "ct" and cts.get(gid) == "running":
        dns = await _probe(spec, f"pct exec {gid} -- timeout 5 getent hosts deb.debian.org")
        if dns[0] is not None and not (dns[1] or "").strip():
            other = next((c for c, st in cts.items() if st == "running" and c != gid), None)
            if other:
                _ok, sibling_config = await _probe(spec, f"pct config {other}")
    elif spec is not None and kind == "vm" and agent_ping is not None and agent_ping[0]:
        from app.modules.assist_local_runner import unwrap_guest_exec
        cmd = f"qm guest exec {gid} -- timeout 5 getent hosts deb.debian.org"
        ok, out = await _probe(spec, cmd)
        dns = (ok, unwrap_guest_exec(cmd, out)) if ok is not None else None
    units_text = ""
    if spec is not None:
        _list = "systemctl list-unit-files --type=service --no-legend"
        if kind == "ct" and ((inventory or {}).get("cts") or {}).get(gid) == "running":
            _ok, units_text = await _probe(spec, f"pct exec {gid} -- {_list}")
            units_text = units_text if _ok else ""
        elif kind == "vm" and agent_ping is not None and agent_ping[0]:
            from app.modules.assist_local_runner import unwrap_guest_exec
            _cmd = f"qm guest exec {gid} -- {_list}"
            _ok, _out = await _probe(spec, _cmd)
            units_text = unwrap_guest_exec(_cmd, _out) if _ok else ""
    t = truth_from_texts(gid, inventory=inventory, qm_config=qm_config or "", neigh=neigh or "", fdb=fdb,
                         agent_ping=agent_ping, plan=plan, dns=dns, sibling_config=sibling_config or "",
                         units_text=units_text)
    logger.warning("machine_truth guest=%s %s", gid, {k: (f"{len(v)} units" if k == "units" and v is not None else v)
                                                      for k, v in t.to_dict().items() if k not in ("reads", "disks")})
    return t


def subject_guest(node: Optional[dict], inventory: Optional[dict] = None) -> Optional[str]:
    """The guest a step is about: by id ("VM 106", "LXC 120") or, §17.1316, by NAME
    against the inventory ("Install PalWorld server" → 106 palworld-server). Live,
    T23 had no id in its text, so no truth was read, no template applied, and the
    draft went over ssh with sudo into a VM whose agent answers."""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description"))
    m = _SUBJECT_RE.search(text)
    if m:
        return m.group(1)
    names = (inventory or {}).get("names") or {}
    hits = [gid for gid, name in names.items() if name and _mentions_guest(text, gid, str(name))]
    return hits[0] if len(hits) == 1 else None


def step_needs(node: Optional[dict], commands: list[str], files: Optional[list[dict]] = None,
               gid: Optional[str] = None) -> set[str]:
    """What the step requires of its subject guest, from the block's shape."""
    gid = gid or subject_guest(node)
    if not gid:
        return set()
    # §17.1308 — the engine's own templates write `GID=120` then `pct exec "$GID"`;
    # read literally, no command named the guest, `needs` was {exists} alone and the
    # stopped-guest row (and its reopen, §17.1307) never fired. Live: CT 120 stayed
    # stopped and ADD110 stayed `done` through three pauses.
    from app.modules.runbook_preconditions import resolve_ids
    texts = [resolve_ids(str(c)) for c in commands or []] + [resolve_ids(str((f or {}).get("content") or "")) for f in files or []]
    joined = "\n".join(texts)
    needs = {"exists"}
    uses_ssh = bool(_SSH_RE.search(joined))
    guest_exec = bool(re.search(rf"\bqm\s+guest\s+exec\s+{gid}\b|\bpct\s+(?:exec|enter)\s+{gid}\b", joined))
    if uses_ssh or guest_exec or re.search(rf"\b(?:qm|pct)\s+status\s+{gid}\b.*running", joined):
        needs.add("running")
    if uses_ssh or guest_exec or re.search(rf"\bqm\s+agent\s+{gid}\b", joined):
        needs.add("has_os")
    if uses_ssh:
        needs.add("reachable")
    if re.search(rf"\bqm\s+guest\s+exec\s+{gid}\b", joined):
        needs.add("agent")
    if (uses_ssh or guest_exec or re.search(rf"\bpct\s+push\s+{gid}\b", joined)) and _NET_FETCH_RE.search(joined):
        needs.add("network")             # §17.1313 — apt/curl/pip/npm… inside the guest need a resolver
    return needs


def contradictions(node: Optional[dict], truth: GuestTruth, needs: set[str],
                   plan: Optional[list[dict]]) -> list[dict]:
    """ONE table. Each row: ``kind``, ``evidence`` (the read, quoted), ``fact``
    (what goes into the ledger, or None), ``reopen`` (a finished plan step whose
    effect the machine does not show, or None), ``covered_by`` (a pending step
    that already supplies the missing effect), ``remedy``."""
    out: list[dict] = []
    plan = plan or []
    gid, name = truth.gid, truth.name

    def _done(pattern: "re.Pattern[str]") -> Optional[dict]:
        return next((n for n in plan if (n.get("status") or "") == "done"
                     and pattern.search(str(n.get("title") or ""))
                     and _mentions_guest(str(n.get("title") or ""), gid, name)), None)

    def _pending(pattern: "re.Pattern[str]") -> Optional[dict]:
        return next((n for n in plan if (n.get("status") or "") in ("pending", "running")
                     and pattern.search(str(n.get("title") or ""))
                     and _mentions_guest(str(n.get("title") or ""), gid, name)), None)

    # The OS: the disk has never been written, yet a finished step installed one.
    if truth.has_os is False:
        names = ", ".join(str(d.get("name")) for d in truth.disks)
        done = _done(_INSTALL_OS_RE)
        cov = _pending(_INSTALL_OS_RE)
        out.append({
            "kind": "record_contradicted:os",
            "evidence": f"`lvs`: {names} Data% 0.00 — never written; no partition table, no OS",
            "fact": (f"{FACT_PREFIX}: {truth.kind or 'guest'} {gid}{' (' + name + ')' if name else ''} has NO operating "
                     f"system: its disk {names} has never been written (lvs Data% 0.00)"
                     + (f"; {done.get('node_key')} '{str(done.get('title'))[:50]}' is recorded done but a later step recreated the disk" if done else "")),
            "reopen": None if (cov or not done) else str(done.get("node_key")),
            "covered_by": str(cov.get("node_key")) if cov else None,
            "blocks": bool({"has_os", "reachable", "agent"} & needs),
            "remedy": (f"install the OS on {gid} first"
                       + (f" — {cov.get('node_key')} '{str(cov.get('title'))[:50]}' is the pending step that does" if cov
                          else (f" — {done.get('node_key')} is reopened to do it again" if done else ""))),
        })
    # Running: the guest is stopped, yet a finished step started it. §17.1307 — the
    # start step is REOPENED (unless this step is itself a start): live, ADD110
    # "Start container 120" stood `done` on a runner error while `pct list` said
    # stopped, and the block that followed (install Caddy, write the Caddyfile)
    # would have started the container and written blind -- the §17.1302 read
    # cannot look inside a stopped guest, so the validated Caddyfile already in
    # it was invisible. Reopening the start puts the running state back under its
    # own approval first; the next pause then reads the step's checks for real.
    if truth.status == "stopped" and "running" in needs:
        done = _done(_START_RE)
        if done:
            this_is_a_start = bool(_START_RE.search(str((node or {}).get("title") or "")))
            kind = "VM" if truth.kind == "vm" else "container"
            out.append({
                "kind": "record_contradicted:start",
                "evidence": f"`{'qm' if truth.kind == 'vm' else 'pct'} list`: {gid} stopped",
                "fact": (None if this_is_a_start else
                         f"{FACT_PREFIX}: {kind} {gid} is stopped (`{'qm' if truth.kind == 'vm' else 'pct'} list`), "
                         f"though {done.get('node_key')} '{str(done.get('title'))[:60]}' is recorded done -- reopened"),
                "reopen": None if this_is_a_start else str(done.get("node_key")),
                "covered_by": None, "blocks": False,
                "remedy": (f"{done.get('node_key')} started it once; it is stopped again — the block starts it, guarded"
                           if this_is_a_start else
                           f"{done.get('node_key')} started it once; it is stopped again — reopened so the start runs first "
                           f"and this step's checks can be read inside the running guest"),
            })
    # §17.1313 — the block fetches by name inside a guest that resolves nothing. The
    # remedy is host-side and the engine has it (a sibling's nameserver): propose
    # the step, make this one wait for it, and refuse the block meanwhile.
    if truth.resolves is False and "network" in needs:
        tool = "qm" if truth.kind == "vm" else "pct"
        kind = "VM" if truth.kind == "vm" else "container"
        hint = truth.dns_hint or "<DNS_SERVER>"
        nums = [int(m.group(1)) for n in plan for m in [re.match(r"ADD(\d+)$", str(n.get("node_key") or ""))] if m]
        new_key = f"ADD{(max(nums) + 1) if nums else 1}"
        cur = str((node or {}).get("node_key") or "")
        out.append({
            "kind": "no_dns",
            "evidence": f"`{tool} exec {gid} -- getent hosts deb.debian.org` printed nothing",
            "fact": f"{FACT_PREFIX}: {kind} {gid} cannot resolve names (getent hosts deb.debian.org printed nothing)"
                    + (f"; sibling guests use nameserver {hint}" if truth.dns_hint else ""),
            "reopen": None, "covered_by": None, "blocks": True,
            "insert": {"node_key": new_key, "title": f"Give {kind} {gid} a working nameserver",
                       "description": (f"Measured: inside {kind} {gid}, `getent hosts deb.debian.org` prints nothing -- it cannot resolve "
                                       f"names, so apt, curl and every download inside it hang until the runner's timeout. "
                                       + (f"Other containers on this host use `nameserver: {hint}` (read from `pct config`). " if truth.dns_hint
                                          else "No sibling guest shows a nameserver to borrow; the operator supplies <DNS_SERVER>. ")
                                       + f"Set it on the host: `{tool} set {gid} --nameserver \"{hint}\"`, then `{tool} reboot {gid}` so the "
                                       f"guest picks it up. Done when `{tool} exec {gid} -- getent hosts deb.debian.org` prints an address."),
                       "depends_on": [], "tool": "LLM"},
            "waits": cur,
            "remedy": f"{kind} {gid} cannot resolve names; {new_key} (inserted) sets `--nameserver {hint}` and this step waits for it",
        })
    # Reachability: a VM that transmits nothing on its bridge is not up, whatever `qm list` says.
    if truth.kind == "vm" and truth.status == "running" and truth.transmits is False and truth.has_os is not False \
            and {"reachable", "agent"} & needs:
        out.append({
            "kind": "not_transmitting",
            "evidence": "`bridge fdb show`: the VM's MAC has never been learned on the bridge",
            "fact": None, "reopen": None, "covered_by": None, "blocks": False,
            "remedy": "the guest has no network stack up yet (still booting, or no OS): wait on the MAC, do not assume",
        })
    return out


#: §17.1315 — work done INSIDE a guest, which a fresh OS wipes: installs, services, configs, users, keys.
_IN_GUEST_WORK_RE = re.compile(r"\b(?:install|configure|set ?up|enable|create|write|add|deploy|run)\b.*\b(?:server|service|unit|daemon|agent|key|user|account|package|docker|steam|game|config)\b|"
                               r"\b(?:service|unit)\b", re.I)
#: host-side work on the guest survives a reinstall: the VM itself, its disk, its start, its config on the host.
_HOST_WORK_RE = re.compile(r"\b(?:create|start|stop|reboot|resize|expand|grow|attach|detach|re-attach|free|give|boot order|passthrough|hostpci|"
                           r"disk|volume|cpu|cores|memory|ram|snapshot|backup|clone|destroy|delete|verify|check|measure|read|capture|console|"
                           r"install (?:ubuntu|debian|the os|an os)|unattended)\b", re.I)


def in_guest_work_voided_by_reinstall(plan: list[dict], gid: str, name: str = "") -> list[dict]:
    """§17.1315 — the DONE steps that did work INSIDE guest ``gid`` before a DONE
    OS install of the same guest finished. Live: VM 106 was reinstalled from a
    cloud image at 05:23 (ADD117); T23 "Install PalWorld server" and T24
    "Configure PalWorld service" (2026-09-04) stood `done` -- and the VM holds
    no steam user, nothing in /opt, nothing on UDP 8211. The operator's primary
    goal, silently absent behind two green records."""
    def _ts(n):
        v = n.get("completed_at")
        return v if v is not None else None
    installs = [n for n in plan or [] if (n.get("status") or "") == "done" and _INSTALL_OS_RE.search(str(n.get("title") or ""))
                and _mentions_guest(str(n.get("title") or ""), gid, name) and _ts(n) is not None]
    if not installs:
        return []
    last = max(installs, key=lambda n: _ts(n) or datetime.min.replace(tzinfo=timezone.utc))
    last_at = _ts(last)
    if last_at is None:
        return []
    out = []
    for n in plan or []:
        at = _ts(n)
        if n is last or (n.get("status") or "") != "done" or at is None or not (at < last_at):
            continue
        title = str(n.get("title") or "")
        if not _mentions_guest(title, gid, name):
            continue
        if _INSTALL_OS_RE.search(title) or _HOST_WORK_RE.search(title) or not _IN_GUEST_WORK_RE.search(title):
            continue
        out.append({"node_key": str(n.get("node_key")), "title": title, "completed_at": _ts(n), "install": str(last.get("node_key")),
                    "install_title": str(last.get("title") or ""), "install_at": _ts(last)})
    return out


async def after_step_done(job_id: str, node: Optional[dict]) -> list[str]:
    """§17.1315 — called when a step is recorded done. When that step installed a
    guest's OS, every earlier done step that worked INSIDE the guest is reopened
    (no cascade) with a measured fact. Returns what was done; fail-soft."""
    title = str((node or {}).get("title") or "")
    if not _INSTALL_OS_RE.search(title):
        return []
    gid = subject_guest(node)
    if not gid:
        return []
    from sqlalchemy import text
    from app.database import async_session
    try:
        async with async_session() as db:
            rows = (await db.execute(text("SELECT node_key, title, status, completed_at FROM dag_nodes WHERE job_id = :j"),
                                     {"j": job_id})).mappings().all()
            plan = [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("after_step_done_plan_failed job=%s err=%r", job_id, exc)
        return []
    return await reopen_voided_work(job_id, plan, gid, guest_name_from_plan(plan, gid))


def guest_name_from_plan(plan: list[dict], gid: str) -> str:
    """§17.1315 — the guest's name as the plan writes it: "Start VM 106 (palworld-server)"."""
    for n in plan or []:
        m = re.search(rf"\b(?:VM|CT|LXC|container|guest)\s*#?\s*{re.escape(gid)}\s*\(([^)]{{2,40}})\)", str(n.get("title") or ""), re.I)
        if m:
            return m.group(1).strip()
    return ""


async def reopen_voided_work(job_id: str, plan: list[dict], gid: str, name: str = "") -> list[str]:
    from app.database import async_session
    from app.modules import node_editor
    from app.modules.assist_environment import set_environment
    from sqlalchemy import text
    voided = in_guest_work_voided_by_reinstall(plan, gid, name)
    done: list[str] = []
    for v in voided:
        evidence = (f"{v['install']} '{v['install_title'][:60]}' reinstalled the OS at {str(v['install_at'])[:16]}; "
                    f"{v['node_key']} '{v['title'][:60]}' was done inside it on {str(v['completed_at'])[:10]}, before that")
        try:
            async with async_session() as db:
                res = await node_editor.reset_node(job_id, v["node_key"], db=db, cascade=False, edited_by=f"engine:measured — {evidence[:140]}")
            if res.get("status") == "ok":
                done.append(f"reopened {v['node_key']}: {v['title'][:70]} (voided by {v['install']})")
                logger.warning("machine_truth_reopened_voided job=%s node=%s by=%s", job_id, v["node_key"], v["install"])
        except Exception as exc:
            logger.warning("machine_truth_reopen_voided_failed job=%s node=%s err=%r", job_id, v["node_key"], exc)
    if done:
        try:
            async with async_session() as db:
                row = (await db.execute(text("SELECT id, metadata FROM assist_sessions WHERE job_id = :j ORDER BY created_at DESC LIMIT 1"),
                                        {"j": job_id})).mappings().first()
                if row:
                    fact = (f"{FACT_PREFIX}: {voided[0]['install']} reinstalled the OS of guest {gid} at {str(voided[0]['install_at'])[:16]}; "
                            f"the in-guest work recorded done before it ({', '.join(v['node_key'] for v in voided)}) is reopened -- a fresh OS holds none of it")
                    await set_environment(session_id=str(row["id"]), facts=[fact], db=db)
        except Exception as exc:
            logger.warning("machine_truth_voided_fact_failed job=%s err=%r", job_id, exc)
    return done


async def reconcile_from_truth(job_id: str, node: Optional[dict], truth: GuestTruth, needs: set[str],
                               plan: Optional[list[dict]]) -> list[str]:
    """Act on the contradictions through the existing primitives, say what was
    done. Facts are deduplicated by their subject prefix; a reopen is
    `reset_node` with no cascade and the evidence as the reason."""
    from sqlalchemy import text
    from app.database import async_session
    from app.modules import node_editor
    from app.modules.assist_environment import set_environment
    done: list[str] = []
    rows = contradictions(node, truth, needs, plan)
    if not rows:
        return done
    facts = [r["fact"] for r in rows if r.get("fact")]
    if facts:
        try:
            async with async_session() as db:
                row = (await db.execute(
                    text("SELECT id, metadata FROM assist_sessions WHERE job_id = :j ORDER BY updated_at DESC LIMIT 1"),
                    {"j": job_id})).mappings().first()
                if row:
                    have = []
                    try:
                        from app.modules.assist_environment import _environment_from_metadata
                        have = [str(f if isinstance(f, str) else (f or {}).get("text") or "")
                                for f in (_environment_from_metadata(row["metadata"]).get("facts") or [])]
                    except Exception:
                        have = []
                    fresh = [f for f in facts if not any(h.startswith(f[:60]) for h in have)]
                    if fresh:
                        await set_environment(session_id=str(row["id"]), facts=fresh, db=db)
                        await db.commit()
                        done.extend(f"fact: {f[:90]}" for f in fresh)
        except Exception as exc:
            logger.warning("machine_truth_fact_failed job=%s err=%r", job_id, exc)
    for r in rows:
        ins = r.get("insert")
        if ins:
            try:
                async with async_session() as db:
                    res = await node_editor.insert_node(job_id, ins, db=db, edited_by=f"engine:measured — {r['evidence'][:120]}")
                if res.get("status") == "ok":
                    done.append(f"inserted {ins['node_key']}: {str(ins.get('title'))[:70]}")
                    logger.warning("machine_truth_inserted job=%s node=%s evidence=%r", job_id, ins["node_key"], r["evidence"][:120])
                    cur = str(r.get("waits") or "")
                    deps = [str(d) for d in ((node or {}).get("depends_on") or [])]
                    if cur and ins["node_key"] not in deps:
                        async with async_session() as db:
                            res2 = await node_editor.edit_node(job_id, cur, {"depends_on": deps + [ins["node_key"]]}, db=db, cascade=False,
                                                              edited_by=f"engine:measured — {cur} needs a resolver {ins['node_key']} provides")
                        if not isinstance(res2, dict) or res2.get("status", "ok") == "ok":
                            if node is not None:
                                node["depends_on"] = deps + [ins["node_key"]]
                            done.append(f"{cur} now waits for {ins['node_key']}")
                else:
                    logger.warning("machine_truth_insert_refused job=%s node=%s res=%r", job_id, ins["node_key"], res)
            except Exception as exc:
                logger.warning("machine_truth_insert_failed job=%s node=%s err=%r", job_id, ins.get("node_key"), exc)
        key = r.get("reopen")
        if not key:
            continue
        try:
            async with async_session() as db:
                res = await node_editor.reset_node(job_id, key, db=db, cascade=False,
                                                   edited_by=f"engine:measured — {r['evidence'][:120]}")
            if res.get("status") == "ok":
                done.append(f"reopened {key}: {r['remedy'][:90]}")
                logger.warning("machine_truth_reopened job=%s node=%s evidence=%r", job_id, key, r["evidence"][:120])
                # §17.1309 — reopening alone changed nothing live: ADD110 went back to
                # pending and the pause still parked ADD88, which comes first in
                # execution order. The step that needs the guest running WAITS for
                # the step that starts it: one dependency, no cascade, and the pause
                # restarts so the start is asked about first.
                cur = str((node or {}).get("node_key") or "")
                deps = [str(d) for d in ((node or {}).get("depends_on") or [])]
                if cur and cur != key and key not in deps:
                    try:
                        async with async_session() as db:
                            res2 = await node_editor.edit_node(job_id, cur, {"depends_on": deps + [key]}, db=db, cascade=False,
                                                              edited_by=f"engine:measured — {cur} needs the guest {key} starts")
                        if not isinstance(res2, dict) or res2.get("status", "ok") == "ok":
                            if node is not None:
                                node["depends_on"] = deps + [key]
                            done.append(f"{cur} now waits for {key}")
                            logger.warning("machine_truth_dependency_added job=%s node=%s waits_for=%s", job_id, cur, key)
                    except Exception as exc:
                        logger.warning("machine_truth_dependency_failed job=%s node=%s waits_for=%s err=%r", job_id, cur, key, exc)
        except Exception as exc:
            logger.warning("machine_truth_reopen_failed job=%s node=%s err=%r", job_id, key, exc)
    return done
