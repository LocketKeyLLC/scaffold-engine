"""§17.1083 — the system map: what the engine's own records say each machine IS.

The session holds the pieces — `system_state` (ids, names, MACs from the
operator's own `config`/`list` output), `substitutions` (pinned IPs), forty
prose `facts` (IPs, listeners, the domain, the WAN address) — and every turn
the model was asked to re-assemble the topology from them. Live (T37,
2026-09-17): it forwarded the Palworld VM's MAC, then the Proxmox host's
8006/22, then agreed that "b4:af:15 at 192.168.1.129" was the Proxmox host
while its own state table said `BC:24:11:B4:AF:15` is VM 110 (ai-vm) and a
fact said the host is static 192.168.1.156. Nothing ever joined them.

This module joins them, deterministically, and renders ONE block at the top
of the environment: one line per machine (id · name · IP · MAC · ports ·
status), the public entry point and the path behind it, and every conflict
the records contain — with the command that settles it. No model call.
"""
from __future__ import annotations

import re
from typing import Any

_IP_RE = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
_MAC_FRAG_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){2,4}[0-9A-Fa-f]{2}\b")
_RES_RE = re.compile(r"\b(?:LXC container|LXC|CT|container|VM|vm)\s+(\d{2,5})\b(?:\s*\(([\w.-]+)\))?", re.IGNORECASE)
_LISTENER_RE = re.compile(r"\bCT\s+(\d{2,5})\s+on\s+(?:\S+:)?(\d{2,5})(?:(?:,|\s+and)\s+(?:\S+:)?(\d{2,5}))*", re.IGNORECASE)
_LISTENER_PORTS_RE = re.compile(r"(?:^|[\s,]|and\s)(?:[\d.*]+|\*):(\d{2,5})\b")
_UPSTREAM_RE = re.compile(r"reverse_proxy\s+(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})")
_HANDLE_RE = re.compile(r"handle_path\s+/([\w-]+)/\*\s*\{\s*reverse_proxy\s+(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})")
_DOMAIN_RE = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+\.(?:org|com|net|dev|io|me|xyz|app))\b", re.IGNORECASE)
_WAN_RE = re.compile(r"\b(?:WAN|public)\s+(?:IPv4|IP)\s+(?:is|=|:)\s*(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)
_HOST_RE = re.compile(r"\b(?:Proxmox host|the host|root@pve|pve)\b", re.IGNORECASE)
_BOUND_IP_RE = re.compile(
    r"(?:\b(?:is at|has (?:the )?IP(?: address)?|IP(?: address)? (?:is|=|of)|address (?:is|=)|inet|reachable at|listening at|→)\s*"
    r"(\d{1,3}(?:\.\d{1,3}){3})\b)", re.IGNORECASE)
_HOST_BOUND_IP_RE = re.compile(
    r"(?:Proxmox host\s*\((\d{1,3}(?:\.\d{1,3}){3})\)|vmbr0[^.]*?\b(?:static|address)\s+(\d{1,3}(?:\.\d{1,3}){3})\b"
    r"|Proxmox host\b[^.]*?\bhas (?:the )?IP(?: address)?\s+(\d{1,3}(?:\.\d{1,3}){3})\b)", re.IGNORECASE)
_NOISE_DOMAINS = ("cloudsmith.io", "github.com", "debian.org", "ubuntu.com", "docker.com", "proxmox.com", "letsencrypt.org", "duckdns.org.")
_MGMT_PORTS = {"22", "8006"}


def _norm_mac(m: str) -> str:
    return m.upper()


def build_system_map(environment: dict | None) -> dict[str, Any]:
    """``{machines: {id: {...}}, host: {...}, ingress: {...}, conflicts: [..]}``.
    Every value carries where it came from so the render can say so."""
    env = environment or {}
    state = env.get("system_state") if isinstance(env.get("system_state"), dict) else {}
    subs = env.get("substitutions") if isinstance(env.get("substitutions"), dict) else {}
    facts = [str(f) for f in (env.get("facts") or []) if str(f).strip()]

    machines: dict[str, dict[str, Any]] = {}

    def m(rid: str) -> dict[str, Any]:
        return machines.setdefault(rid, {"id": rid, "kind": None, "name": None, "ips": {}, "macs": set(),
                                         "ports": set(), "status": None, "sources": set(), "addressing": None})

    # 1. the structured records — ids, names, MACs, status
    for rid, rec in state.items():
        if not isinstance(rec, dict) or str(rid) == "host" or rec.get("kind") == "host":
            continue           # the host is rendered on its own line (2b)
        r = m(str(rid))
        r["kind"] = rec.get("kind") or r["kind"]
        attrs = rec.get("attrs") or {}
        r["name"] = attrs.get("name") or attrs.get("hostname") or r["name"]
        if attrs.get("status"):
            r["status"] = str(attrs["status"]).lower()
        for dev, val in (rec.get("devices") or {}).items():
            if str(dev).startswith("net"):
                for mac in _MAC_RE.findall(str(val)):
                    r["macs"].add(_norm_mac(mac))
                # §17.1086 — how the machine gets its address, from its own
                # config line: `ip=dhcp` → leased by the router; `ip=A/24` →
                # static, the router never hears from it; neither → set inside
                # the guest (not leased unless the guest itself runs DHCP)
                mip = re.search(r"\bip=([^,\s]+)", str(val))
                if mip:
                    v = mip.group(1).lower()
                    r["addressing"] = "dhcp (leased by the router)" if v == "dhcp" else f"static {mip.group(1)} (set in Proxmox — the router never leases to it)"
                elif r["addressing"] is None:
                    r["addressing"] = "configured inside the guest (no ip= on net0; not router-leased unless the guest runs DHCP)"
        r["sources"].add(str(rec.get("source") or "config"))

    # 2. prose facts — resource mentions with IPs / MACs / ports / status
    host_ips: dict[str, str] = {}
    listeners: dict[str, set[str]] = {}
    gone: set[str] = set()
    for f in facts:
        # a sentence is read CLAUSE by clause ("VM 106 … is stopped; VM 110 …
        # is running") so one clause's status/IP never lands on the other's id
        for clause in re.split(r"\s*;\s*", f):
            ids = {x for x, _ in _RES_RE.findall(clause)}
            for rid, name in _RES_RE.findall(clause):
                r = m(rid)
                if name and not r["name"]:
                    r["name"] = name
            if len(ids) != 1:
                continue
            r = m(next(iter(ids)))
            low = clause.lower()
            if re.search(r"\b(?:fully removed|removed|destroyed|deleted|no longer exists)\b", low):
                gone.add(r["id"])
                continue
            # an IP binds to a resource ONLY through a phrasing that says so
            # ("CT 120 … is at / has IP / inet 192.168.1.26"); an address that
            # merely appears in the same clause (a curl target, an upstream,
            # a neighbour lookup) does not. The first cut bound every address
            # in the sentence and gave caddy four IPs.
            for ip in _BOUND_IP_RE.findall(clause):
                r["ips"].setdefault(ip, f[:90])
            for mac in _MAC_RE.findall(clause):
                r["macs"].add(_norm_mac(mac))
            if re.search(r"\b(?:is|status:?|now|is now)\s+(?:running|stopped)\b", low):
                r["status"] = "stopped" if "stopped" in low else "running"
        # listeners: "Container listeners: CT 101 on 0.0.0.0:8096, CT 120 on *:443 and *:80"
        for seg in re.split(r"[;]", f):
            for rid_l, first in re.findall(r"\bCT\s+(\d{2,5})\s+on\s+((?:[\d.*]+:\d{2,5}(?:\s*(?:,|and)\s*)?)+)", seg, re.IGNORECASE):
                ports = set(re.findall(r":(\d{2,5})", first))
                listeners.setdefault(rid_l, set()).update(ports)
        # the Proxmox host's own address — only when the sentence SAYS it is
        # the host's ("Proxmox host (192.168.1.156)", "vmbr0 … static X/24",
        # "the host with MAC … has IP address X")
        if "not the Proxmox host" in f:      # §17.1084 — a fact the engine already re-read
            continue
        for tup in _HOST_BOUND_IP_RE.findall(f):
            ip = next((x for x in tup if x), None)
            if ip:
                host_ips.setdefault(ip, f[:90])
    for rid, ports in listeners.items():
        m(rid)["ports"].update(ports)

    # 2b. structured IPs (from `ip -4 addr` parses) outrank prose
    for rid, rec in state.items():
        if isinstance(rec, dict) and (rec.get("attrs") or {}).get("ip"):
            # observed wins the SOURCE label for its address (a prose fact that
            # names the same address is corroboration, not the record)
            if str(rid) == "host" or rec.get("kind") == "host":
                host_ips = {**host_ips, rec["attrs"]["ip"]: f"observed via {rec.get('source', 'ip addr')}"}
                host_ips = {rec["attrs"]["ip"]: host_ips[rec["attrs"]["ip"]], **{k: v for k, v in host_ips.items() if k != rec["attrs"]["ip"]}}
            else:
                r = m(str(rid))
                ip = rec["attrs"]["ip"]
                r["ips"] = {ip: f"observed via {rec.get('source', 'ip addr')}", **{k: v for k, v in r["ips"].items() if k != ip}}
    # 3. pinned IPs by name (JELLYFIN_IP → jellyfin)
    pins_by_name: dict[str, str] = {}
    for k, v in subs.items():
        if str(k).upper().endswith("_IP") and _IPV4_RE.fullmatch(str(v).strip()):
            pins_by_name[str(k)[:-3].lower().replace("_", "-")] = str(v).strip()
    pin_conflicts: list[dict[str, Any]] = []
    for r in machines.values():
        nm = (r["name"] or "").lower()
        if nm and nm in pins_by_name and pins_by_name[nm] not in r["ips"]:
            observed = [ip for ip, src in r["ips"].items() if src.startswith("observed via")]
            if observed:
                # §17.1085 — a pin the operator typed disagrees with what their own
                # machine printed: say so rather than list two addresses as equals
                pin_conflicts.append({"kind": "pin_vs_observed", "id": r["id"], "name": r["name"],
                                      "pinned": pins_by_name[nm], "observed": observed[0]})
            else:
                r["ips"][pins_by_name[nm]] = f"pinned {nm.upper().replace('-', '_')}_IP"
    # the host too (PVE_IP / HOST_IP pins)
    for key in ("PVE_IP", "HOST_IP", "PROXMOX_IP"):
        v = str(subs.get(key) or "").strip()
        if v and _IPV4_RE.fullmatch(v):
            observed_host = [ip for ip, src in host_ips.items() if src.startswith("observed via")]
            if observed_host and v not in host_ips:
                pin_conflicts.append({"kind": "pin_vs_observed", "id": "host", "name": "Proxmox host", "pinned": v, "observed": observed_host[0]})
            elif v not in host_ips:
                host_ips[v] = f"pinned {key}"

    # 4. the ingress path: domain, WAN, entry point (who listens on 80/443), upstreams
    dom_counts: dict[str, int] = {}
    for f in facts:
        for d in _DOMAIN_RE.findall(f):
            dl = d.lower()
            if dl.startswith("www.") or any(dl.endswith(x) for x in _NOISE_DOMAINS):
                continue
            weight = 3 if re.search(r"\b(?:ACME|certificate|https://|duckdns|DNS|resolves)\b", f, re.IGNORECASE) else 1
            dom_counts[dl] = dom_counts.get(dl, 0) + weight
    for k, v in subs.items():
        if str(k).upper() in ("DOMAIN", "FQDN", "HOSTNAME") and _DOMAIN_RE.fullmatch(str(v).strip()):
            dom_counts[str(v).strip().lower()] = dom_counts.get(str(v).strip().lower(), 0) + 5
    domain = max(dom_counts, key=dom_counts.get) if dom_counts else None
    wan = next((w for f in facts for w in _WAN_RE.findall(f)), None)
    upstreams: dict[str, str] = {}
    for f in facts:
        for path, target in _HANDLE_RE.findall(f):
            upstreams[path] = target
        if not upstreams:
            for ip, port in _UPSTREAM_RE.findall(f):
                upstreams.setdefault(f"{ip}:{port}", f"{ip}:{port}")
    entry = next((r for r in machines.values() if {"80", "443"} & r["ports"]), None)
    # join upstream targets to machines by port (CT 101 :8096 ↔ 192.168.1.20:8096)
    for target in list(upstreams.values()):
        ip, _, port = target.partition(":")
        for r in machines.values():
            if port in r["ports"] and not r["ips"]:
                r["ips"][ip] = f"upstream {target} joined on port {port}"

    # 5. conflicts — one machine with several IPs; the host recorded at two
    # addresses; a MAC fragment that names another machine
    conflicts: list[dict[str, Any]] = list(pin_conflicts)
    for r in machines.values():
        if len(r["ips"]) > 1:
            conflicts.append({"kind": "ip", "id": r["id"], "name": r["name"], "values": dict(r["ips"]),
                              "settle": (f"pct exec {r['id']} -- ip -4 addr show eth0" if r["kind"] == "ct"
                                         else f"qm guest cmd {r['id']} network-get-interfaces")})
    if len(host_ips) > 1:
        entry_c = {"kind": "host_ip", "values": dict(host_ips), "settle": "ip -4 addr show vmbr0", "resolutions": []}
        for f_text in host_ips.values():
            for frag in _MAC_FRAG_RE.findall(f_text):
                owner = _owner_of_mac_fragment(frag, machines)
                if owner is not None:
                    entry_c["resolutions"].append({"fragment": frag, "id": owner["id"], "name": owner["name"]})
        conflicts.append(entry_c)
    # any fact that ties a MAC fragment to a machine it does not belong to
    for f in facts:
        for frag in _MAC_FRAG_RE.findall(f):
            owner = _owner_of_mac_fragment(frag, machines)
            if owner is None:
                continue
            claimed = _RES_RE.findall(f)
            if _HOST_RE.search(f) and not claimed:
                conflicts.append({"kind": "mac_owner", "fragment": frag, "fact": f[:140], "id": owner["id"], "name": owner["name"]})
            for rid, _ in claimed:
                if rid != owner["id"]:
                    conflicts.append({"kind": "mac_owner", "fragment": frag, "fact": f[:140], "id": owner["id"], "name": owner["name"]})

    for rid in gone:
        machines.pop(rid, None)
    return {
        "machines": {k: {**v, "macs": sorted(v["macs"]), "ports": sorted(v["ports"], key=int), "sources": sorted(v["sources"])}
                     for k, v in sorted(machines.items(), key=lambda kv: int(kv[0]))},
        "gone": sorted(gone),
        "host": {"ips": host_ips},
        "ingress": {"domain": domain, "wan": wan, "entry_id": entry["id"] if entry else None,
                    "entry_ip": next(iter(entry["ips"]), None) if entry else None, "upstreams": upstreams},
        "conflicts": conflicts,
    }


def _owner_of_mac_fragment(frag: str, machines: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    f = _norm_mac(frag)
    hits = [r for r in machines.values() if any(mac.endswith(f) or mac.startswith(f) for mac in r["macs"])]
    return hits[0] if len(hits) == 1 else None


def render_system_map(environment: dict | None) -> str:
    """The prompt block. Empty when the records hold no machine at all."""
    sm = build_system_map(environment)
    machines = sm["machines"]
    if not machines and not sm["host"]["ips"]:
        return ""
    lines = ["### SYSTEM MAP (joined from the operator's own records — use THESE names, ids, addresses and MACs; "
             "never a generic example like 'container 101' or '192.168.1.50' when the real one is here)"]
    if sm["host"]["ips"]:
        ips = list(sm["host"]["ips"])
        lines.append(f"- Proxmox host (root@pve): {ips[0]}" + (f"  ⚠ ALSO recorded as {', '.join(ips[1:])} — see CONFLICTS" if len(ips) > 1 else ""))
    for rid, r in machines.items():
        kind = "VM" if r["kind"] == "vm" else "CT"
        bits = [f"{kind} {rid}" + (f" ({r['name']})" if r["name"] else "")]
        if r["ips"]:
            bits.append("IP " + " / ".join(r["ips"]) + (" ⚠" if len(r["ips"]) > 1 else ""))
        if r["macs"]:
            bits.append("MAC " + ", ".join(f"{mac} (…:{':'.join(mac.split(':')[-3:])})" for mac in r["macs"]))
        if r["ports"]:
            bits.append("listens " + ",".join(r["ports"]))
        if r.get("addressing"):
            bits.append("addressing: " + r["addressing"])
        if r["status"]:
            bits.append(r["status"])
        lines.append("- " + " · ".join(bits))
    ing = sm["ingress"]
    if ing["entry_id"] or ing["domain"] or ing["upstreams"]:
        ent = machines.get(ing["entry_id"] or "")
        path = ["Internet"]
        if ing["domain"]:
            path.append(ing["domain"])
        if ing["wan"]:
            path.append(f"WAN {ing['wan']} (router)")
        if ent:
            path.append(f"router port-forward TCP 80,443 → {ing['entry_ip'] or '?'} = CT {ent['id']} ({ent['name'] or 'reverse proxy'})"
                        + (f", MAC {ent['macs'][0]}" if ent["macs"] else ""))
        if ent and ent.get("addressing") and not ent["addressing"].startswith("dhcp"):
            lines_extra = (f"  ENTRY POINT ADDRESSING: {ent['addressing']} — a router that builds its device / reservation / "
                           f"forwarding list from DHCP leases has never seen this machine; it must lease once (then reserve) before it can be chosen.")
        else:
            lines_extra = ""
        lines.append("")
        lines.append("INGRESS PATH: " + " → ".join(path))
        if lines_extra:
            lines.append(lines_extra)
        if ing["upstreams"]:
            lines.append("  behind it (the proxy routes to these; they are NOT forwarded from the router): "
                         + "; ".join(f"/{p} → {t}" if not p.count(":") else t for p, t in ing["upstreams"].items()))
        lines.append("  RULE: the router forwards ONLY to the entry point above, ONLY ports 80/443. Never forward "
                     "22 or 8006 (management) to the internet, and never point a forward at a VM/CT that is not the entry point.")
    if sm["conflicts"]:
        lines.append("")
        lines.append("CONFLICTS in the records (settle before acting on either value; do not silently pick one):")
        for c in sm["conflicts"]:
            if c["kind"] == "ip":
                lines.append(f"- {('VM' if machines[c['id']]['kind'] == 'vm' else 'CT')} {c['id']} ({c['name'] or '?'}) has two IPs on record: "
                             + " vs ".join(f"{ip} (from: {src})" for ip, src in c["values"].items()) + f" → settle with `{c['settle']}`")
            elif c["kind"] == "host_ip":
                lines.append("- the Proxmox host is recorded at " + " AND ".join(f"{ip} (from: {src})" for ip, src in c["values"].items())
                             + f" → settle with `{c['settle']}`")
                for res in c.get("resolutions", []):
                    lines.append(f"  · the MAC fragment {res['fragment']} in that fact belongs to {'VM' if machines[res['id']]['kind'] == 'vm' else 'CT'} "
                                 f"{res['id']} ({res['name'] or '?'}), not to the host — the address is probably that machine's")
            elif c["kind"] == "pin_vs_observed":
                lines.append(f"- pinned value says {c['name']} is {c['pinned']} but its own `ip addr` output says {c['observed']} — "
                             "the observed address is what the machine has NOW; fix the pin or re-check the machine, do not use both")
            elif c["kind"] == "mac_owner":
                lines.append(f"- MAC {c['fragment']} belongs to {'VM' if machines[c['id']]['kind'] == 'vm' else 'CT'} {c['id']} ({c['name'] or '?'}) "
                             f"per its config — a fact assigns it elsewhere: \"{c['fact']}\"")
    return "\n".join(lines)


def topology_of(environment: dict | None) -> dict | None:
    """The system map for the answer gates; None when nothing is known."""
    try:
        sm = build_system_map(environment)
        return sm if (sm.get("machines") or sm.get("host", {}).get("ips")) else None
    except Exception:  # noqa: BLE001 — derived; never breaks an answer
        return None


def resolve_mac_fragments(text_value: str, environment: dict | None) -> list[dict[str, Any]]:
    """MAC fragments in an operator message → the machine that owns them
    (for the turn's own prompt: 'b4:af:15 is VM 110 (ai-vm)')."""
    sm = build_system_map(environment)
    out = []
    for frag in dict.fromkeys(_MAC_FRAG_RE.findall(text_value or "")):
        owner = _owner_of_mac_fragment(frag, sm["machines"])
        if owner is not None:
            out.append({"fragment": frag, "id": owner["id"], "name": owner["name"], "kind": owner["kind"]})
    return out


# ---------------------------------------------------------------------------
# §17.1083b — the ingress gate. A prompt rule is a request (§17.882); with the
# map, the recap and the operator's own words all saying "Caddy, 80/443", the
# live replay still handed out VM 106's MAC and UDP 8211, and the day before
# it forwarded 8006/22 to the host. This is enforcement: a draft that points
# a forward/reservation at a machine that is not the entry point, or at a
# management port, is caught, regenerated with the map's line spelled out,
# and — if it still fails — labelled so the operator sees the disagreement.
# ---------------------------------------------------------------------------

_FWD_CTX_RE = re.compile(
    r"port[- ]?forward\w*|port assignment|reserve[d]?\s+ip|ip reservation|external port|internal port|"
    r"forward(?:ing)?\s+(?:tcp|udp|port|ports)\b", re.IGNORECASE)
_GATE_RES_RE = re.compile(
    r"\b(?:LXC container|LXC|CT|container|VM|vm)\s+(\d{2,5})\b|\b(?:qm|pct)\s+(?:config|set|exec)\s+(\d{2,5})\b"
    r"|\b(\d{2,5})\s*\((?:[A-Za-z][\w-]*)\)", re.IGNORECASE)
_PORT_MENTION_RE = re.compile(r"\b(?:port|ports|external port|internal port|tcp|udp)\b[:\s`]*(\d{2,5})\b|\b(\d{2,5})\s*/\s*(?:tcp|udp)\b", re.IGNORECASE)


def ingress_issues(answer: str, topology: dict | None, *, focus: str = "") -> list[dict[str, Any]]:
    """Forwarding instructions in ``answer`` that contradict the map's entry
    point. Empty when the map has no entry point or the answer does not talk
    about forwarding.

    ``focus`` — §17.1086: what the TURN is about (the operator's question plus
    the step recap's OPEN/NEXT lines). When the focus names the entry point
    (its ip, name or domain, or ports 80/443), every forwarding window in the
    draft is about the entry point — a draft that then names another machine
    ("e.g., 101 (Jellyfin)") is wrong even though the draft itself never says
    "caddy". The first cut judged each window from the draft alone and let
    exactly that through."""
    if not answer or not topology:
        return []
    ing = topology.get("ingress") or {}
    entry_id, entry_ip, domain = ing.get("entry_id"), ing.get("entry_ip"), ing.get("domain")
    machines = topology.get("machines") or {}
    if not entry_id:
        return []
    entry = machines.get(entry_id) or {}
    entry_names = {n for n in ((entry.get("name") or "").lower(),) if n}
    known_ips = {ip for r in machines.values() for ip in r.get("ips", {})} | set((topology.get("host") or {}).get("ips", {}))
    issues: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    flow = (focus or "").lower()
    focus_on_entry = bool(flow) and bool((entry_ip and entry_ip in flow) or (domain and domain.lower() in flow)
                                         or any(n in flow for n in entry_names)
                                         or re.search(r"\b(?:80|443)\b", " ".join(p for t in _PORT_MENTION_RE.findall(focus or "") for p in t if p)))

    def add(kind: str, **kw):
        key = (kind, tuple(sorted(kw.items())))
        if key not in seen:
            seen.add(key)
            issues.append({"kind": kind, **kw})

    for m in _FWD_CTX_RE.finditer(answer):
        window = answer[max(0, m.start() - 350): m.end() + 350]
        low = window.lower()
        about_entry = focus_on_entry or bool((entry_ip and entry_ip in window) or (domain and domain.lower() in low)
                                             or any(n in low for n in entry_names)
                                             or re.search(r"\b(?:80|443)\b", " ".join(p for t in _PORT_MENTION_RE.findall(window) for p in t if p)))
        # (a) a management port in a forwarding context — wrong regardless
        for t in _PORT_MENTION_RE.findall(window):
            port = next((p for p in t if p), "")
            if port in _MGMT_PORTS:
                add("mgmt_port", port=port)
        # (b) another machine named where the entry point is the subject
        for t in _GATE_RES_RE.findall(window):
            rid = next((x for x in t if x), "")
            if rid and rid != entry_id and rid in machines and about_entry:
                add("wrong_machine", id=rid, name=machines[rid].get("name"))
        for rid, r in machines.items():
            nm = (r.get("name") or "").lower()
            stem = nm.split("-")[0] if nm else ""
            if rid != entry_id and about_entry and nm and (nm in low or (len(stem) >= 5 and re.search(rf"\b{re.escape(stem)}\b", low))):
                add("wrong_machine", id=rid, name=r.get("name"))
        # (c) a forward aimed at another known address while the entry is the subject
        if about_entry:
            for ip in _IPV4_RE.findall(window):
                if ip in known_ips and ip != entry_ip and re.search(rf"(?:to|at|target|→|points? (?:at|to)|reserved ip[^.]*?)\s*`?{re.escape(ip)}", window, re.IGNORECASE):
                    add("wrong_target", ip=ip)
    return issues


def ingress_notice(issues: list[dict[str, Any]], topology: dict) -> str:
    """The regeneration directive, with the map's line spelled out."""
    if not issues:
        return ""
    ing = topology["ingress"]; ent = (topology.get("machines") or {}).get(ing["entry_id"]) or {}
    kind = "VM" if ent.get("kind") == "vm" else "CT"
    line = (f"{kind} {ing['entry_id']} ({ent.get('name') or 'reverse proxy'}) at {ing.get('entry_ip') or '?'}"
            + (f", MAC {ent['macs'][0]}" if ent.get("macs") else "") + ", TCP ports 80 and 443")
    wrong = []
    for i in issues:
        if i["kind"] == "wrong_machine":
            wrong.append(f"NOT {'VM' if (topology['machines'].get(i['id']) or {}).get('kind') == 'vm' else 'CT'} {i['id']} ({i.get('name') or '?'})")
        elif i["kind"] == "mgmt_port":
            wrong.append(f"NEVER port {i['port']} (management — not to the internet)")
        elif i["kind"] == "wrong_target":
            wrong.append(f"NOT {i['ip']}")
    return ("\n\n---\nINGRESS NOTICE: the port forward / IP reservation this answer describes must target the plan's "
            f"public entry point and nothing else: {line}. " + "; ".join(dict.fromkeys(wrong))
            + ". Rewrite the answer with exactly those names, that address, that MAC and those ports; "
            "if the operator's router lists devices by MAC, the MAC to find is the entry point's.")


def ingress_footer(issues: list[dict[str, Any]], topology: dict) -> str:
    if not issues:
        return ""
    ing = topology["ingress"]; ent = (topology.get("machines") or {}).get(ing["entry_id"]) or {}
    kind = "VM" if ent.get("kind") == "vm" else "CT"
    what = "; ".join(dict.fromkeys(
        (f"points a forward at {'VM' if (topology['machines'].get(i['id']) or {}).get('kind') == 'vm' else 'CT'} {i['id']} ({i.get('name') or '?'})" if i["kind"] == "wrong_machine"
         else f"forwards management port {i['port']}" if i["kind"] == "mgmt_port"
         else f"targets {i['ip']}") for i in issues))
    return (f"\n\n---\n⚠️ **Wrong target** — this answer {what}. The plan's public entry point is "
            f"{kind} {ing['entry_id']} ({ent.get('name') or '?'}) at {ing.get('entry_ip') or '?'}"
            + (f" (MAC {ent['macs'][0]})" if ent.get("macs") else "") + ", ports 80/443 only. Treat the forwarding steps above with suspicion.")


# ---------------------------------------------------------------------------
# §17.1084 — reconcile a NEW fact against the records before it is written.
#
# "The Proxmox host with MAC prefix b4:af:15 has IP address 192.168.1.129"
# went into the ledger verbatim while the state table said BC:24:11:B4:AF:15
# is VM 110. §17.1083 flags that at read time; this corrects it at WRITE
# time, deterministically: a MAC fragment that resolves to one machine names
# that machine, an IP bound in the same clause becomes that machine's
# structured IP, and the fact is rewritten to say so — the operator's words
# are kept, the engine's reading is appended.
# ---------------------------------------------------------------------------

_FACT_IP_RE = re.compile(r"\b(?:has|is at|IP(?: address)?(?: is)?|address(?: is)?)\s*(\d{1,3}(?:\.\d{1,3}){3})\b", re.IGNORECASE)


def reconcile_fact(fact: str, environment: dict | None) -> tuple[str, dict | None]:
    """``(fact_to_store, structured_update | None)``. The update is a
    ``system_state`` observation (``{id: {kind, attrs: {ip}, source}}``) when
    the fact binds an address to a machine the records can name."""
    text_value = (fact or "").strip()
    if not text_value:
        return text_value, None
    sm = build_system_map(environment)
    machines = sm.get("machines") or {}
    if not machines:
        return text_value, None
    frags = list(dict.fromkeys(_MAC_FRAG_RE.findall(text_value)))
    resolved = [(frag, _owner_of_mac_fragment(frag, machines)) for frag in frags]
    resolved = [(f, o) for f, o in resolved if o is not None]
    if not resolved:
        return text_value, None
    frag, owner = resolved[0]
    named = {x for x, _ in _RES_RE.findall(text_value)}
    if owner["id"] in named and len(named) == 1:
        # the fact already names the machine the MAC belongs to — consistent;
        # still bind an address it states, as structure
        ip_m = _FACT_IP_RE.search(text_value)
        if ip_m:
            return text_value, {owner["id"]: {"kind": owner.get("kind") or "vm", "attrs": {"ip": ip_m.group(1)},
                                              "devices": {}, "source": f"fact reconciled ({frag})"}}
        return text_value, None
    kind = "VM" if owner.get("kind") == "vm" else "CT"
    label = f"{kind} {owner['id']}" + (f" ({owner['name']})" if owner.get("name") else "")
    ip_m = _FACT_IP_RE.search(text_value)
    claimed_host = bool(_HOST_RE.search(text_value)) and not _RES_RE.search(text_value)
    note = f" [engine: MAC {frag} is {label} per its config"
    if claimed_host:
        note += ", not the Proxmox host"
    update = None
    if ip_m:
        ip = ip_m.group(1)
        note += f"; so {ip} is {label}'s address"
        update = {owner["id"]: {"kind": owner.get("kind") or "vm", "attrs": {"ip": ip}, "devices": {},
                                "source": f"fact reconciled ({frag})"}}
    return text_value.rstrip(".") + note + "].", update


# ---------------------------------------------------------------------------
# §17.1091 — the PREREQUISITE gate on the entry point.
#
# A certificate failure at the reverse proxy while the ledger records that
# the router does not yet reach it is EXPECTED, not a config defect; the
# proxy's config already validated. Live: three truncate-and-rewrite rounds
# of a valid Caddyfile because the symptom (`tlsv1 alert internal error`)
# was local and the write ledger had a stale size. This names the unmet
# prerequisite from the records and refuses a draft that rewrites the entry
# point's files instead of directing the operator to it.
# ---------------------------------------------------------------------------

_INGRESS_UNMET_RE = re.compile(
    r"(?:\b(?:acme|certificate|cert|challenge|let'?s encrypt)\b[^\n]{0,160}?\b(?:fails?|failed|timeout|timed out|firewall|refused|unreachable|cannot|could not)\b"
    r"|\b(?:port[- ]?forward\w*|reservation|reserve)\b[^\n]{0,120}?\b(?:blocked|would not allow|not allowed|cannot|can'?t|does not list|not listed|not see|doesn'?t see)\b"
    r"|\b(?:blocked|cannot|can'?t|would not allow|does not list|not listed)\b[^\n]{0,80}?\b(?:port[- ]?forward\w*|reservation|reserve)\b"
    r"|\bnever leases\b|\brouter (?:has never seen|does not see|cannot see)\b)", re.IGNORECASE)
_INGRESS_MET_RE = re.compile(r"\b(?:certificate (?:issued|obtained|renewed)|https? (?:works|reachable) from outside|port[- ]?forward\w* (?:is )?(?:working|verified|confirmed|in place))\b", re.IGNORECASE)
_TLS_SYMPTOM_RE = re.compile(r"\b(?:tls|ssl|https|certificate|cert|acme|handshake|alert internal error)\b", re.IGNORECASE)
_CONFIG_VALID_RE = re.compile(r"\b(?:valid configuration|validates successfully|config(?:uration)? (?:is )?(?:ok|valid)|syntax is ok|test is successful)\b", re.IGNORECASE)
_WRITE_CMD_RE = re.compile(r"\b(?:truncate\s+-s\s*0|tee\s+(?:-a\s+)?/|cat\s*>>?\s*/|sed\s+-i|>\s*/etc/|rm\s+(?:-f\s+)?/etc/|caddy fmt --overwrite)", re.IGNORECASE)


def ingress_prerequisite(environment: dict | None) -> dict[str, Any] | None:
    """``{entry_id, entry_name, entry_ip, evidence, config_validated}`` when the
    records say the router does not yet reach the entry point (and nothing
    says it does); else None. Deterministic; read from facts and the map."""
    sm = build_system_map(environment)
    ing = sm.get("ingress") or {}
    if not ing.get("entry_id"):
        return None
    facts = [str(f) for f in ((environment or {}).get("facts") or [])]
    unmet = [f for f in facts if _INGRESS_UNMET_RE.search(f)]
    met = [f for f in facts if _INGRESS_MET_RE.search(f)]
    if not unmet or met:
        return None
    ent = (sm.get("machines") or {}).get(ing["entry_id"]) or {}
    addressing = ent.get("addressing") or ""
    if addressing and not addressing.startswith("dhcp"):
        unmet.append(f"entry point addressing: {addressing}")
    return {"entry_id": ing["entry_id"], "entry_name": ent.get("name"), "entry_ip": ing.get("entry_ip"),
            "entry_mac": (ent.get("macs") or [None])[0], "evidence": unmet[:4],
            "config_validated": any(_CONFIG_VALID_RE.search(f) for f in facts)}


def prerequisite_issues(draft: str, environment: dict | None, *, focus: str = "") -> list[dict[str, Any]]:
    """A FIX draft that rewrites/edits files on the entry point while the
    router prerequisite is unmet and the symptom in focus is TLS/HTTPS."""
    if not draft:
        return []
    pre = ingress_prerequisite(environment)
    if not pre:
        return []
    if not _TLS_SYMPTOM_RE.search(f"{focus}\n{draft[:400]}"):
        return []
    eid = pre["entry_id"]
    on_entry = re.search(rf"\b(?:pct|qm)\s+exec\s+{re.escape(eid)}\b[^\n]*", draft) is not None
    writes = [m.group(0) for m in _WRITE_CMD_RE.finditer(draft)]
    if not (on_entry and writes):
        return []
    return [{"kind": "prerequisite_unmet", "entry_id": eid, "entry_name": pre["entry_name"], "writes": writes[:4],
             "evidence": pre["evidence"], "config_validated": pre["config_validated"]}]


def prerequisite_notice(issues: list[dict[str, Any]], environment: dict | None) -> str:
    if not issues:
        return ""
    pre = ingress_prerequisite(environment) or {}
    i = issues[0]
    ev = "; ".join(str(e)[:110] for e in (i.get("evidence") or [])[:3])
    valid = " Its configuration already validated — it is not the defect." if i.get("config_validated") else ""
    return ("\n\n---\nPREREQUISITE NOTICE: the symptom is EXPECTED until the router forwards TCP 80/443 to "
            f"CT {i['entry_id']} ({i.get('entry_name') or 'the entry point'}"
            + (f", {pre.get('entry_ip')}" if pre.get("entry_ip") else "") + (f", MAC {pre.get('entry_mac')}" if pre.get("entry_mac") else "")
            + f"). The records say that prerequisite is not met: {ev}.{valid} Do NOT rewrite, truncate or edit its files. "
            "Rewrite the answer to say the certificate/HTTPS check cannot pass yet and direct the operator to completing the router "
            "forward first (making the entry point visible to the router if it is not, reserving its address, adding TCP 80 and 443), "
            "then re-running this check.")


def prerequisite_footer(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return ""
    i = issues[0]
    return (f"\n\n---\n⚠️ **Prerequisite not met** — this answer edits files on CT {i['entry_id']} ({i.get('entry_name') or '?'}) "
            "while the records say the router does not yet forward 80/443 to it; the HTTPS failure is expected until that is done"
            + (" and the configuration already validated" if i.get("config_validated") else "") + ". Treat the edits above with suspicion.")
