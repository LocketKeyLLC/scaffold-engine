"""§17.1481 — a step that documents or validates the build is given the build, read off the machines.

Live (2026-10-11): T38 "Document architecture and setup", rewritten after §17.1480 with every measured fact in reach,
was correct wherever the engine had measured something and filled the rest from the original plan and general
knowledge: Palworld "deployment via Docker" on LAN-only ports (it is native SteamCMD on VM 106 at 192.168.1.44 with
UDP 8211 open to all), "public reachability is confirmed from outside the LAN" (nobody had tested it), a "removal of
old containers" procedure, and no host address, no Jellyfin, download client, Pi-hole or AI VM at all. Its prompt
held the plan's text and the facts; nothing in it was the inventory as it stands.

`measured_inventory(spec)` reads it — the host, every container and VM with its status, address and how it gets it,
its passed-through devices and what it listens on — and `inventory_block` renders it with the rule that every
machine, address, port and service the step states comes from it (or the facts), else is written "not verified".
Read-only and fail-soft: a machine that cannot be read is listed as unread, never guessed.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("scaffold")

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_LAN_RE = re.compile(r"^(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.")
_DOC_RE = re.compile(r"\b(?:document(?:ation|s)?|write[- ]?up|architecture|as[- ]built|handover|hand[- ]off)\b", re.I)
_PORT_RE = re.compile(r"^(tcp|udp)\s+\S+\s+\d+\s+\d+\s+(\S+):(\d+)\s", re.M)
MAX_GUESTS = 16


def documents_or_validates(node: Optional[dict]) -> bool:
    from app.modules.runbook_templates import _WHOLE_SYSTEM_RE
    title = str((node or {}).get("title") or "")
    return bool(_DOC_RE.search(title) or _WHOLE_SYSTEM_RE.search(title))


def _lan(text: str) -> list[str]:
    return [ip for ip in dict.fromkeys(_IPV4_RE.findall(text or "")) if _LAN_RE.match(ip)]


def _listeners(ss: str) -> str:
    seen: list[str] = []
    for proto, addr, port in _PORT_RE.findall(ss or ""):
        if addr.startswith(("127.", "[::1]")) or addr == "::1":
            continue
        p = f"{port}/{proto}"
        if p not in seen:
            seen.append(p)
    return ", ".join(sorted(seen, key=lambda x: (x.split("/")[1], int(x.split("/")[0]))))


def _kv(conf: str) -> dict[str, str]:
    return {k.strip(): v.strip() for k, v in (ln.split(":", 1) for ln in (conf or "").splitlines() if ":" in ln)}


def _how(net0: str) -> str:
    m = re.search(r"\bip=([^,]+)", net0 or "")
    if not m:
        return ""
    return "DHCP" if m.group(1) == "dhcp" else "fixed"


async def measured_inventory(spec) -> list[dict]:
    from app.modules.service_truth import _probe
    out: list[dict] = []

    async def read(cmd: str) -> str:
        ok, t = await _probe(spec, cmd)
        return t if ok else ""
    host_ips = _lan(await read("hostname -I"))
    out.append({"kind": "host", "id": "host", "name": (await read("hostname")).strip() or "pve",
                "status": "running", "address": ", ".join(host_ips) or "unread",
                "listens": _listeners(await read("ss -tulnH"))})
    for kind, lst in (("ct", await read("pct list")), ("vm", await read("qm list"))):
        for ln in (lst or "").splitlines()[1:][:MAX_GUESTS]:
            parts = ln.split()
            if not parts or not parts[0].isdigit():
                continue
            gid = parts[0]
            if kind == "ct":
                status, name = (parts[1] if len(parts) > 1 else "?"), (parts[-1] if len(parts) > 2 else "")
            else:
                name, status = (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "?")
            conf = _kv(await read(f"{'pct' if kind == 'ct' else 'qm'} config {gid}"))
            net0 = conf.get("net0", "")
            how = _how(net0) if kind == "ct" else _how(conf.get("ipconfig0", "")) or ""
            rec = {"kind": kind, "id": gid, "name": conf.get("hostname") or conf.get("name") or name,
                   "status": status, "address": "", "how": how,
                   "devices": ", ".join(f"{k}={v}" for k, v in conf.items() if k.startswith(("hostpci", "mp"))),
                   "listens": ""}
            if status == "running":
                if kind == "ct":
                    rec["address"] = ", ".join(_lan(await read(f"pct exec {gid} -- hostname -I")))
                    rec["listens"] = _listeners(await read(f"pct exec {gid} -- ss -tulnH"))
                elif conf.get("agent", "").startswith("1"):
                    rec["address"] = ", ".join(_lan(await read(f"qm guest exec {gid} -- hostname -I")))
                    rec["listens"] = _listeners(await read(f"qm guest exec {gid} -- ss -tulnH"))
            if not rec["address"]:
                m = re.search(r"\bip=(\d[\d.]+)", net0 or conf.get("ipconfig0", ""))
                rec["address"] = (m.group(1) + " (configured)") if m else ("unread" if status == "running" else "")
            rec["firewall"] = guest_firewall(net0, await read(f"cat /etc/pve/firewall/{gid}.fw"))
            out.append(rec)
    # §17.1485 — the security groups, where Proxmox keeps them
    groups = re.findall(r"^\s*\[group\s+([\w-]+)\]", await read("cat /etc/pve/firewall/cluster.fw") or "", re.M | re.I)
    if groups:
        out[0]["groups"] = (", ".join(groups) + " — defined in /etc/pve/firewall/cluster.fw, which is where Proxmox "
                            "keeps security groups (it has no per-group files and no groups/ directory)")
    # §17.1482 — the tunnel and the BMC, which the write-up otherwise reports from stale facts as "pending"
    ts = tailscale_summary(await read("tailscale status --json"))
    if ts:
        out[0]["tailscale"] = ts
    wd = re.search(r"Watchdog Timer Is:\s*(.+)", await read("ipmitool mc watchdog get") or "")
    if wd:
        out[0]["watchdog"] = wd.group(1).strip()
    return out


def guest_firewall(net0: str, fw: str) -> str:
    """§17.1482 — "on (policy_in DROP; IN ACCEPT udp 8211 from any, …; GROUP game)" off the guest's own file.
    Live, T38 wrote "security groups … not yet materialized as per-guest group files … pending" from a stale fact
    while every guest's net0 carried firewall=1 and /etc/pve/firewall/106.fw opened UDP 8211 to everyone."""
    on = "firewall=1" in (net0 or "")
    if not (fw or "").strip() or re.search(r"No such file or directory", fw or ""):
        return "on (no rules file)" if on else "off"
    enabled = re.search(r"^\s*enable:\s*1\b", fw, re.M) is not None
    pol = re.search(r"^\s*policy_in:\s*(\w+)", fw, re.M)
    rules = []
    for ln in fw.splitlines():
        ln = ln.strip()
        m = re.match(r"IN\s+ACCEPT\s+-p\s+(\w+)\s+-dport\s+(\S+)(?:\s+-source\s+(\S+))?", ln)
        if m:
            rules.append(f"{m.group(2)}/{m.group(1)} from {m.group(3) or 'anywhere'}")
        elif ln.startswith("GROUP "):
            rules.append(f"group {ln.split()[1]}")
    state = "on" if (on and enabled) else ("enabled in its file, off on net0" if enabled else "off")
    detail = ([f"policy_in {pol.group(1)}"] if pol else []) + rules
    return state + (f" ({'; '.join(detail)})" if detail else "")


def tailscale_summary(js: str) -> str:
    import json
    try:
        d = json.loads(js or "{}")
    except (ValueError, TypeError):
        return ""
    if not d:
        return ""
    me = d.get("Self") or {}
    routes = me.get("PrimaryRoutes") or []
    peers = [p.get("HostName") for p in (d.get("Peer") or {}).values() if isinstance(p, dict)]
    return (f"{d.get('BackendState') or '?'}, {', '.join((d.get('TailscaleIPs') or [])[:1]) or '?'}"
            + (f"; serving subnet route {', '.join(routes)} (approved)" if routes else "; no subnet route approved")
            + (f"; {len(peers)} other device(s): {', '.join(p for p in peers if p)}" if peers else "; no other devices"))


def inventory_block(inv: list[dict]) -> str:
    if not inv:
        return ""
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    for r in inv:
        label = "Proxmox host" if r["kind"] == "host" else f"{'CT' if r['kind'] == 'ct' else 'VM'} {r['id']}"
        bits = [f"{label} ({r.get('name') or '?'})", r.get("status") or "?"]
        if r.get("address"):
            bits.append(f"address {r['address']}" + (f" ({r['how']})" if r.get("how") else ""))
        if r.get("devices"):
            bits.append(f"devices {r['devices']}")
        if r.get("listens"):
            bits.append(f"listens on {r['listens']}")
        if r.get("firewall"):
            bits.append(f"firewall {r['firewall']}")
        if r.get("groups"):
            bits.append(f"firewall security groups {r['groups']}")
        if r.get("tailscale"):
            bits.append(f"Tailscale {r['tailscale']}")
        if r.get("watchdog"):
            bits.append(f"BMC watchdog {r['watchdog']}")
        lines.append("- " + " · ".join(bits))
    return ("\n\nTHE BUILD AS IT STANDS — read off the machines at " + when + " (read-only). Every machine, "
            "address, port and service you state must come from this list, the engine's measured facts or the "
            "upstream outputs. Where a fact, an upstream output OR A NOTE IN THIS STEP'S OWN TEXT (a \"[Plan "
            "update …]\" written before the work was done) disagrees with this list about what exists or is in "
            "place, THIS LIST IS NEWER AND WINS — do not report as pending, missing or still to be created what it "
            "shows in place. Anything you cannot source from them, write "
            "as \"not verified\" — never fill it from the original plan or general knowledge, and never describe "
            "planned work as done:\n"
            + "\n".join(lines))


async def block_for(node: Optional[dict], db) -> str:
    """The inventory block for a documenting/validating step, '' otherwise or when no runner can be read."""
    if not documents_or_validates(node):
        return ""
    try:
        from app.modules import supervised_runs as sr
        ch = await sr.channel(db)
        if ch is None:
            return ""
        inv = await measured_inventory(ch[0])
        logger.warning("inventory_report_read node=%s machines=%d", (node or {}).get("node_key"), len(inv))
        return inventory_block(inv)
    except Exception as exc:
        logger.warning("inventory_report_failed node=%s err=%r", (node or {}).get("node_key"), exc)
        return ""
