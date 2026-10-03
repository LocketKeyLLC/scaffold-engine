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
    reads: dict[str, str] = field(default_factory=dict)   # evidence, by read

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "reads"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _mentions_guest(title: str, gid: str, name: str) -> bool:
    return bool(re.search(rf"\b{re.escape(gid)}\b", title)) or (bool(_norm(name)) and _norm(name) in _norm(title))


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
                     plan: Optional[list[dict]] = None) -> GuestTruth:
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
    t = truth_from_texts(gid, inventory=inventory, qm_config=qm_config or "", neigh=neigh or "", fdb=fdb,
                         agent_ping=agent_ping, plan=plan)
    logger.warning("machine_truth guest=%s %s", gid, {k: v for k, v in t.to_dict().items() if k not in ("reads", "disks")})
    return t


def subject_guest(node: Optional[dict]) -> Optional[str]:
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description"))
    m = _SUBJECT_RE.search(text)
    return m.group(1) if m else None


def step_needs(node: Optional[dict], commands: list[str], files: Optional[list[dict]] = None) -> set[str]:
    """What the step requires of its subject guest, from the block's shape."""
    gid = subject_guest(node)
    if not gid:
        return set()
    texts = [str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []]
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
    # Running: the guest is stopped, yet a finished step started it (cheap; the block starts it itself).
    if truth.status == "stopped" and "running" in needs:
        done = _done(_START_RE)
        if done:
            out.append({
                "kind": "record_contradicted:start",
                "evidence": f"`{'qm' if truth.kind == 'vm' else 'pct'} list`: {gid} stopped",
                "fact": None, "reopen": None, "covered_by": None, "blocks": False,
                "remedy": f"{done.get('node_key')} started it once; it is stopped again — the block starts it, guarded",
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
        except Exception as exc:
            logger.warning("machine_truth_reopen_failed job=%s node=%s err=%r", job_id, key, exc)
    return done
