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
                                         "ports": set(), "status": None, "sources": set()})

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
            if str(rid) == "host" or rec.get("kind") == "host":
                host_ips = {rec["attrs"]["ip"]: f"observed via {rec.get('source', 'ip addr')}", **host_ips}
            else:
                r = m(str(rid))
                r["ips"] = {rec["attrs"]["ip"]: f"observed via {rec.get('source', 'ip addr')}", **r["ips"]}
    # 3. pinned IPs by name (JELLYFIN_IP → jellyfin)
    pins_by_name: dict[str, str] = {}
    for k, v in subs.items():
        if str(k).upper().endswith("_IP") and _IPV4_RE.fullmatch(str(v).strip()):
            pins_by_name[str(k)[:-3].lower().replace("_", "-")] = str(v).strip()
    for r in machines.values():
        nm = (r["name"] or "").lower()
        if nm and nm in pins_by_name and pins_by_name[nm] not in r["ips"]:
            r["ips"][pins_by_name[nm]] = f"pinned {nm.upper().replace('-', '_')}_IP"

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
    conflicts: list[dict[str, Any]] = []
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
        lines.append("")
        lines.append("INGRESS PATH: " + " → ".join(path))
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
    r"\b(?:LXC container|LXC|CT|container|VM|vm)\s+(\d{2,5})\b|\b(?:qm|pct)\s+config\s+(\d{2,5})\b", re.IGNORECASE)
_PORT_MENTION_RE = re.compile(r"\b(?:port|ports|external port|internal port|tcp|udp)\b[:\s`]*(\d{2,5})\b|\b(\d{2,5})\s*/\s*(?:tcp|udp)\b", re.IGNORECASE)


def ingress_issues(answer: str, topology: dict | None) -> list[dict[str, Any]]:
    """Forwarding instructions in ``answer`` that contradict the map's entry
    point. Empty when the map has no entry point or the answer does not talk
    about forwarding."""
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

    def add(kind: str, **kw):
        key = (kind, tuple(sorted(kw.items())))
        if key not in seen:
            seen.add(key)
            issues.append({"kind": kind, **kw})

    for m in _FWD_CTX_RE.finditer(answer):
        window = answer[max(0, m.start() - 350): m.end() + 350]
        low = window.lower()
        about_entry = bool((entry_ip and entry_ip in window) or (domain and domain.lower() in low)
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
