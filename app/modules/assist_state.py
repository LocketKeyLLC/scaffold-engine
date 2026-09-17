"""§17.914 — a durable, STRUCTURED model of the operator's system.

The root cause behind §17.906-913, stated plainly: the engine has a PLAN (what
should happen) and a TRANSCRIPT (what was said), and no model of what the
operator's machine actually IS. Every turn it re-derives the world from prose,
or asks again.

Measured on the live session (613dd1df): the engine asked for `qm config 106`
**21 times**; the operator pasted the answer **6 times**. The environment had
nowhere to keep it — only free-text `profile`, LLM-distilled prose `facts`, and
small scalar lists. So the ground truth arrived, was read for exactly one turn,
and was thrown away.

Every gate built in §17.906-913 compensates for that absence rather than
removing it. §17.907 in particular *instructs* the engine to ask for state,
which is precisely why it asked twenty-one times.

This module parses the operator's OWN pasted command output into structured
resource records. Deterministic — no model call, no judgment. A parser only
fires on output it can recognise unambiguously, and records where each value
came from so a stale reading can be told from a fresh one.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("scaffold.assist_state")

# `qm config 106` / `pct config 104` — the command echo tells us the id AND the
# kind, and the body is a flat `key: value` block. Anchored to the prompt echo
# so we never parse a block the operator did not actually run.
_CONFIG_ECHO_RE = re.compile(
    r"(?:^|\n)([^\n]*?)\b(qm|pct)\s+config\s+(\d{2,5})\b([^\n]*)\n(.*?)(?=\n[^\n]*[$#]\s|\n\s*==\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)
# §17.1083 — a config echo is trusted only when the config command is the
# LAST command on its line (a `| grep …` filter is fine); an echoed SCRIPT
# (`pct config 101 | grep '^mp0'; pct exec 101 -- …` followed by twenty more
# probes) is not an echo of one command, and the text after it is not that
# resource's configuration. Live: the state-check probe script attributed
# every probe's output to CT 101 — palworld-server's name, prowlarr's
# hostname, VM 106's MAC and three other resources' disks — and that block
# outranks the prose facts in every prompt.
_TRAILING_COMMAND_RE = re.compile(r"[;&]|\|\s*(?!grep\b|egrep\b|head\b|tail\b|sort\b|cat\b)")
# A line that is itself another command echo or a probe marker ends a body.
_BODY_BREAK_RE = re.compile(r"^\s*(?:==\s|\S+@\S+[:~][^$#]*[$#]\s|(?:qm|pct|pvesm|zpool|zfs|lvs|ls|cat|ssh|curl)\s)")
_KV_RE = re.compile(r"^([a-z][a-z0-9_]{1,20}):\s*(.+?)\s*$", re.IGNORECASE)
# `qm list` / `pct list` tables — id, name, status for EVERY resource in one
# read; the operator pastes them constantly and they were never kept.
_QM_LIST_ROW_RE = re.compile(r"^[ \t]*(\d{2,5})[ \t]+(\S+)[ \t]+(running|stopped|paused|suspended)\b", re.IGNORECASE | re.MULTILINE)
_PCT_LIST_ROW_RE = re.compile(r"^[ \t]*(\d{2,5})[ \t]+(running|stopped)[ \t]+(?:\S+[ \t]+)?(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_NET_SET_ECHO_RE = re.compile(
    r"(?:^|\n)[^\n]*[$#]\s[^\n]*\b(pct|qm)\s+(?:create|set)\s+(\d{2,5})\b[^\n]*?--?net0[= ]+(\S+)", re.IGNORECASE)
_EXEC_IPADDR_RE = re.compile(
    r"\bpct\s+exec\s+(\d{2,5})\s+--\s+ip\s+(?:-4\s+)?(?:addr|address|a)\b[^\n]*\n((?:[^\n]*\n?){1,6})", re.IGNORECASE)
_HOST_IPADDR_RE = re.compile(
    r"(?:^|\n)[^\n]*\bip\s+(?:-4\s+)?(?:addr|address|a)\s+show\s+vmbr0[^\n]*\n(?:[^\n]*\n){0,3}?[^\n]*\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/\d+", re.IGNORECASE)
_IFACE_STATIC_RE = re.compile(
    r"iface\s+vmbr0\s+inet\s+static\s*\n\s*address\s+(\d{1,3}(?:\.\d{1,3}){3})(?:/\d+)?", re.IGNORECASE)
_QM_LIST_HEAD_RE = re.compile(r"^\s*VMID\s+NAME\s+STATUS", re.IGNORECASE | re.MULTILINE)
_PCT_LIST_HEAD_RE = re.compile(r"^\s*VMID\s+Status\s+Lock\s+Name", re.IGNORECASE | re.MULTILINE)

# Values worth keeping: the ones the engine kept guessing at.
_CONFIG_KEYS = frozenset({
    "boot", "name", "memory", "cores", "ostype", "scsihw", "onboot", "arch",
    "hostname", "rootfs", "cpu", "machine", "bios", "agent", "status", "ip", "_listed_name",
})
_DEVICE_KEY_RE = re.compile(r"^(?:scsi|ide|sata|virtio|net|efidisk|tpmstate|mp|unused)\d+$",
                            re.IGNORECASE)


def parse_system_state(operator_text: str) -> dict[str, dict[str, Any]]:
    """Structured resource records from pasted command output.

    Returns ``{resource_id: {kind, attrs, devices, source}}``. Empty when the
    text contains nothing recognisable — this never guesses.
    """
    out: dict[str, dict[str, Any]] = {}
    text_in = operator_text or ""
    for m in _CONFIG_ECHO_RE.finditer(text_in):
        before, verb, rid, after, body = m.group(1), m.group(2).lower(), m.group(3), m.group(4), m.group(5)
        # An echoed script, not a command: refuse the whole match.
        if _TRAILING_COMMAND_RE.search(after) or "$(" in before or " for " in f" {before} " or "do " in before:
            logger.info("system_state_echo_refused rid=%s reason=script_line", rid)
            continue
        attrs: dict[str, str] = {}
        devices: dict[str, str] = {}
        seen_keys: set[str] = set()
        for line in body.splitlines():
            stripped = line.strip()
            if _BODY_BREAK_RE.match(line) and not _KV_RE.match(stripped):
                break
            kv = _KV_RE.match(stripped)
            if not kv:
                continue
            key, val = kv.group(1).lower(), kv.group(2).strip()
            if key in seen_keys:
                break          # a key repeating means a SECOND resource's output began
            seen_keys.add(key)
            if _DEVICE_KEY_RE.match(key):
                devices[key] = val
            elif key in _CONFIG_KEYS:
                attrs[key] = val
        # §17.1083 — contamination guard: a disk that belongs to another id
        # cannot be this resource's; drop the record rather than store a lie.
        foreign = [k for k, v in {**attrs, **devices}.items()
                   if re.search(r"\bvm-(\d{2,5})-", str(v)) and re.search(r"\bvm-(\d{2,5})-", str(v)).group(1) != rid]
        if foreign:
            logger.warning("system_state_record_refused rid=%s foreign_disks=%s", rid, foreign)
            continue
        if attrs or devices:
            out[rid] = {
                "kind": "vm" if verb == "qm" else "ct",
                "attrs": attrs,
                "devices": devices,
                "source": f"{verb} config {rid}",
            }
    # §17.1083 — `pct exec N -- ip -4 addr show eth0` → the container's IP, as
    # STRUCTURE. The prose distiller dropped exactly this (the live ledger held
    # "ip neigh | grep 192.168.1.26 returns nothing" and never "CT 120 is
    # 192.168.1.26"), and the map cannot be built without it.
    for rid, body in _EXEC_IPADDR_RE.findall(text_in):
        ips = re.findall(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/\d+", body)
        if ips:
            rec = out.setdefault(rid, {"kind": "ct", "attrs": {}, "devices": {}, "source": f"pct exec {rid} -- ip addr"})
            rec["attrs"]["ip"] = ips[0]
    # §17.1086 — `pct create N … --net0 …ip=…` / `pct set N -net0 …` / `qm set N
    # -net0 …` echoed on a prompt line: the addressing the operator gave the
    # machine (static vs dhcp) is the fact behind "the router can't see it".
    for verb, rid, spec in _NET_SET_ECHO_RE.findall(text_in):
        spec = spec.strip().strip("'\"")
        if "ip=" in spec or "hwaddr=" in spec or "bridge=" in spec:
            rec = out.setdefault(rid, {"kind": "vm" if verb.lower() == "qm" else "ct", "attrs": {}, "devices": {},
                                       "source": f"{verb.lower()} net0 echo"})
            rec["devices"]["net0"] = spec
    # the host's own bridge address, from `ip -4 addr show vmbr0` or the
    # interfaces file
    host_ip = None
    mh = _HOST_IPADDR_RE.search(text_in)
    if mh:
        host_ip = mh.group(1)
    else:
        mi = _IFACE_STATIC_RE.search(text_in)
        if mi:
            host_ip = mi.group(1)
    if host_ip:
        rec = out.setdefault("host", {"kind": "host", "attrs": {}, "devices": {}, "source": "ip addr / interfaces"})
        rec["attrs"]["ip"] = host_ip
    # `qm list` / `pct list` — name + status per id. A list row never
    # overrides a config record's attrs; it fills name/status where absent.
    if _QM_LIST_HEAD_RE.search(text_in):
        for rid, name, status in _QM_LIST_ROW_RE.findall(text_in):
            rec = out.setdefault(rid, {"kind": "vm", "attrs": {}, "devices": {}, "source": "qm list"})
            rec["attrs"]["name"] = name
            rec["attrs"]["_listed_name"] = name
            rec["attrs"]["status"] = status.lower()
    if _PCT_LIST_HEAD_RE.search(text_in):
        for rid, status, name in _PCT_LIST_ROW_RE.findall(text_in):
            rec = out.setdefault(rid, {"kind": "ct", "attrs": {}, "devices": {}, "source": "pct list"})
            rec["attrs"]["hostname"] = name
            rec["attrs"]["_listed_name"] = name
            rec["attrs"]["status"] = status.lower()
    from app.modules.assist_gates import run_gate
    clean, _ = run_gate("state_invariants", reconcile_system_state, out, default=(out, []))
    return clean


def _merge_devices(cur: dict, new: dict) -> dict:
    """§17.1086 — a net line is `k=v,k=v`; a partial read (a `pct set -net0
    ip=dhcp` echo) updates the keys it carries and keeps the rest (the
    hwaddr the config read established) instead of replacing the line."""
    out = dict(cur)
    for k, v in new.items():
        if str(k).startswith("net") and k in out and "=" in str(v) and "=" in str(out[k]):
            def _kv(spec: str) -> dict:
                return dict(p.split("=", 1) for p in str(spec).split(",") if "=" in p)
            m = {**_kv(out[k]), **_kv(v)}
            out[k] = ",".join(f"{a}={b}" for a, b in m.items())
        else:
            out[k] = v
    return out


def merge_system_state(current: dict | None, observed: dict) -> dict:
    """Newer observation wins per resource; untouched resources survive."""
    merged = {k: v for k, v in (current or {}).items() if isinstance(v, dict)}
    for rid, rec in (observed or {}).items():
        cur = merged.get(rid)
        partial = " config " not in f" {rec.get('source', '')} "
        if cur and partial:
            # §17.1083 — a PARTIAL read (a `list` row, an `ip addr` line)
            # refreshes what it observed on the existing record — newer wins
            # for the keys it carries — and never deletes the devices/attrs a
            # config read established.
            cur_is_config = " config " in f" {cur.get('source', '')} "
            merged[rid] = {**cur,
                           "attrs": {**(cur.get("attrs") or {}), **(rec.get("attrs") or {})},
                           "devices": _merge_devices(cur.get("devices") or {}, rec.get("devices") or {}),
                           "source": cur.get("source") if cur_is_config else rec.get("source", cur.get("source"))}
            continue
        if cur and not partial and " config " not in f" {cur.get('source', '')} ":
            # a config read arriving over a partial record keeps the partial's ip/status
            keep = {k: v for k, v in (cur.get("attrs") or {}).items() if k in ("ip", "status") and k not in (rec.get("attrs") or {})}
            rec = {**rec, "attrs": {**(rec.get("attrs") or {}), **keep}}
        merged[rid] = rec
    from app.modules.assist_gates import run_gate
    clean, _ = run_gate("state_invariants", reconcile_system_state, merged, default=(merged, []))   # §17.1084
    return dict(list(clean.items())[-40:])


def render_system_state(state: dict | None) -> str:
    """The prompt block. This is the half that makes the ledger real — a record
    the model cannot see is a record the engine does not have (§17.913)."""
    rows = [(rid, rec) for rid, rec in (state or {}).items() if isinstance(rec, dict)]
    if not rows:
        return ""
    # §17.917 — SCOPE. The first version of this header said only "GROUND TRUTH
    # … do NOT contradict it", and the model drew a conclusion the data never
    # supported. Live (session 613dd1df, turn 1445): "Guide me" on ADD5
    # "Install Ubuntu Server 22.04 on VM 106" produced an entirely POST-INSTALL
    # walkthrough — fix the boot order, detach the ISO, "wait for the login
    # prompt" — because this block showed `boot: order=scsi0` and a 100G
    # `scsi0` disk. A disk existing is not an OS existing. Worse, the §17.714
    # reset branch had just demoted the facts that said the install was HUNG to
    # "earlier observations … most will not hold", so the one authoritative
    # block in the prompt was a CONFIGURATION snapshot, and configuration
    # outranked observation.
    #
    # A `qm config` read establishes what the hypervisor is configured to do.
    # It establishes nothing about what is installed, running, or working
    # inside the guest. Say so, in the block itself.
    lines = [
        "### CONFIRMED resource CONFIGURATION (read from the operator's own "
        "command output — accurate for what it covers; do NOT ask them to "
        "re-run a command whose answer is already here, and do NOT contradict "
        "these values).\n"
        "SCOPE — this is hypervisor configuration ONLY. It does NOT establish "
        "that any OS or software is installed, booted, running or working "
        "inside these resources: a disk being attached is not an OS being "
        "installed on it. Never infer that a step's goal is already achieved "
        "from configuration alone."
    ]
    for rid, rec in sorted(rows):
        if rec.get("kind") == "host":
            continue           # §17.1083 — rendered by the system map
        kind = "VM" if rec.get("kind") == "vm" else "container"
        attrs = rec.get("attrs") or {}
        devices = rec.get("devices") or {}
        head = f"- {kind} {rid}"
        if attrs.get("name") or attrs.get("hostname"):
            head += f" ({attrs.get('name') or attrs.get('hostname')})"
        lines.append(head + f"  [via `{rec.get('source', '?')}`]")
        for k in sorted(attrs):
            if k in ("name", "hostname") or k.startswith("_"):
                continue
            lines.append(f"    - {k}: {attrs[k]}")
        for d in sorted(devices):
            lines.append(f"    - {d}: {devices[d]}")
    return "\n".join(lines)


# §17.914 — asking for state you already hold. Storing and rendering the state
# is necessary but NOT sufficient: with the CONFIRMED block in the prompt, the
# live model still opened with `qm config 106` and still asserted "Boot Order is
# currently set to prioritize the virtual CD-ROM" while the block said
# `boot: order=scsi0`. Prompt rules are guidance; this is enforcement — the
# §17.668/882 lesson applied to state.
_DISCOVERY_FOR_RE = re.compile(
    r"\b(qm|pct)\s+config\s+(\d{2,5})\b", re.IGNORECASE)


def find_redundant_discovery(text_out: str, state: dict | None) -> list[dict]:
    """Discovery commands in the draft whose answer is already in `state`.

    Returns ``[{command, resource, known}]`` — `known` is a compact rendering of
    what is already on file, so the regeneration directive can name it.
    """
    if not state or not (text_out or "").strip():
        return []
    hits: list[dict] = []
    seen: set[str] = set()
    for block in re.findall(r"```[a-z]*\n(.*?)```", text_out, re.DOTALL):
        for m in _DISCOVERY_FOR_RE.finditer(block):
            rid = m.group(2)
            rec = state.get(rid)
            if not isinstance(rec, dict) or rid in seen:
                continue
            seen.add(rid)
            attrs = rec.get("attrs") or {}
            devices = rec.get("devices") or {}
            known = ", ".join(
                f"{k}={v}" for k, v in list(attrs.items())[:4]
            ) or ", ".join(f"{k}={v}" for k, v in list(devices.items())[:3])
            hits.append({
                "command": f"{m.group(1).lower()} config {rid}",
                "resource": rid,
                "known": known,
            })
    return hits


# ---------------------------------------------------------------------------
# §17.1084 — INVARIANTS on the state table, enforced at every write.
#
# §17.1083 fixed the parse that contaminated CT 101; this refuses the RESULT
# of any such parse, whatever its cause. Three things are physically true of
# a Proxmox inventory and were never checked:
#   1. a MAC address belongs to exactly one machine;
#   2. a disk `vm-N-…` belongs to machine N;
#   3. a `pct list` / `qm list` row names its machine (a config record whose
#      hostname/name disagrees with the newest list row is the wrong record).
# A record that breaks one is repaired where the offending field can be
# dropped, refused where it cannot, and every repair is logged — nothing
# impossible reaches a prompt.
# ---------------------------------------------------------------------------

_MAC_IN_DEV_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
_VM_ONLY_KEYS = frozenset({"name", "bios", "machine", "agent", "scsihw"})
_CT_ONLY_KEYS = frozenset({"hostname", "arch", "rootfs", "unprivileged"})
_DISK_OWNER_RE = re.compile(r"\bvm-(\d{2,5})-")


def reconcile_system_state(state: dict | None) -> tuple[dict, list[dict]]:
    """``(clean_state, repairs)``. Deterministic; idempotent on a clean table."""
    st = {k: dict(v) for k, v in (state or {}).items() if isinstance(v, dict)}
    repairs: list[dict] = []
    # 4. form ↔ kind: a container's net line is `name=eth0,…,hwaddr=…`, a
    # VM's is `virtio=…|e1000=…|vmxnet3=…`; and the config KEYS differ (`name`,
    # `bios`, `machine`, `agent`, `scsihw` are qm-only; `hostname`, `arch`,
    # `rootfs`, `unprivileged` are pct-only). A field of the other kind's form
    # on a record is another machine's output, whatever brought it here.
    for rid, rec in st.items():
        kind = rec.get("kind")
        if kind not in ("ct", "vm"):
            continue
        attrs = dict(rec.get("attrs") or {})
        for k in list(attrs):
            if (kind == "ct" and k in _VM_ONLY_KEYS) or (kind == "vm" and k in _CT_ONLY_KEYS):
                repairs.append({"kind": "wrong_kind_key", "id": rid, "field": k})
                attrs.pop(k)
        rec["attrs"] = attrs
        devs = dict(rec.get("devices") or {})
        for k, v in list(devs.items()):
            if not str(k).startswith("net"):
                continue
            vm_form = bool(re.match(r"\s*(?:virtio|e1000|e1000e|vmxnet3|rtl8139)=", str(v), re.IGNORECASE))
            ct_form = "hwaddr=" in str(v).lower()
            if (kind == "ct" and vm_form) or (kind == "vm" and ct_form):
                repairs.append({"kind": "wrong_kind_net", "id": rid, "field": k})
                devs.pop(k)
        rec["devices"] = devs
    # 2. disks — the owner id is in the name
    for rid, rec in list(st.items()):
        if rec.get("kind") == "host":
            continue
        for bucket in ("devices", "attrs"):
            vals = dict(rec.get(bucket) or {})
            for k, v in list(vals.items()):
                m = _DISK_OWNER_RE.search(str(v))
                if m and m.group(1) != str(rid):
                    repairs.append({"kind": "foreign_disk", "id": rid, "field": k, "owner": m.group(1)})
                    vals.pop(k)
            rec[bucket] = vals
    # 1. MACs — one owner. The record whose `source` is a config read of THAT
    # id keeps it; any other holder loses the device line.
    holders: dict[str, list[str]] = {}
    for rid, rec in st.items():
        for k, v in (rec.get("devices") or {}).items():
            for mac in _MAC_IN_DEV_RE.findall(str(v)):
                holders.setdefault(mac.upper(), []).append(rid)
    for mac, rids in holders.items():
        if len(set(rids)) < 2:
            continue
        owners = [r for r in set(rids) if f" config {r}" in f" {st[r].get('source', '')}"]
        keep = owners[0] if len(owners) == 1 else None
        for rid in set(rids):
            if rid == keep:
                continue
            devs = {k: v for k, v in (st[rid].get("devices") or {}).items() if mac not in str(v).upper()}
            repairs.append({"kind": "shared_mac", "id": rid, "mac": mac, "kept_on": keep})
            st[rid]["devices"] = devs
    # 3. names — the newest list row is authoritative for name/hostname
    for rid, rec in st.items():
        attrs = rec.get("attrs") or {}
        listed = attrs.get("_listed_name")
        current_name = attrs.get("hostname") or attrs.get("name")
        if listed and current_name != listed:
            if current_name is not None:
                repairs.append({"kind": "name_mismatch", "id": rid, "config": current_name, "list": listed})
            attrs["hostname" if rec.get("kind") == "ct" else "name"] = listed
    # a record the repairs emptied is refused outright (an empty record that
    # arrived empty is left alone — it is a placeholder, not a lie)
    touched = {r["id"] for r in repairs}
    for rid in [r for r, rec in st.items() if r in touched and not (rec.get("attrs") or rec.get("devices"))]:
        repairs.append({"kind": "empty_after_repair", "id": rid})
        st.pop(rid)
    if repairs:
        logger.warning("system_state_reconciled repairs=%s", repairs[:8])
    return st, repairs
