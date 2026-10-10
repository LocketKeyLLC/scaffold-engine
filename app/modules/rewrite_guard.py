"""§17.1457 — a block may change what its step is about, and nothing it would lose on the way.

Live (ADD151, 2026-10-10, "Proxmox firewall: enforce the security groups on every guest"), the drafted block
carried, among correct per-guest firewall files:

  printf '%s\n' '[group dmz]' 'IN ACCEPT -p tcp -dport 80 …' … | tee /etc/pve/firewall/cluster.fw
      — the WHOLE cluster firewall file replaced by three lines: the firewall's own `enable: 1`, its DROP
        policy and the management/media/game/ai groups gone;
  pct set 101 -net0 name=eth0,bridge=vmbr0,firewall=1,ip=dhcp        (and 102–130 alike)
      — every container's fixed address, gateway and MAC dropped: each would come back on DHCP with a new
        MAC, a new address, and every reservation, port-forward and proxy target pointing at nothing;
  qm set 106 -net0 virtio=BC:24:11:00:00:06,bridge=vmbr0,firewall=1
      — a MAC the drafter made up.

The step's own text said "keep every other net0 field exactly as it is". Both checks read the LIVE machine
(the engine's recorded net0s were missing for five containers and wrong for one), so a rewrite is judged
against what is there now:

- `net0_rewrite`: a `pct set N --net0 …` / `qm set N --net0 …` may change `firewall` and nothing else that
  the current net0 holds;
- `file_overwrite`: a `… | tee FILE` / `> FILE` / a written file may not drop lines FILE holds now.

Fail-soft on blindness: a guest or file the runner cannot read is not refused (that is not evidence).
"""
from __future__ import annotations

import logging
import re
import shlex
from typing import Optional

logger = logging.getLogger("scaffold")

NET0_MARKER = "is the only field this rewrite of net0 may change"
FILE_MARKER = "lines it holds now would be lost"

_NET0_SET_RE = re.compile(r"\b(pct|qm)\s+set\s+(\d{3,5})\b[^\n]*?\s-{1,2}net0[\s=]+('[^']*'|\"[^\"]*\"|\S+)")
_TEE_RE = re.compile(r"\btee\s+(?!-a\b|--append\b)((?:-\S+\s+)*)(/[^\s'\"|;&)]+)")
_REDIRECT_RE = re.compile(r"(?<![>0-9&])>\s*(/[^\s'\"|;&)]+)")
_IN_GUEST_RE = re.compile(r"\b(?:pct\s+exec|qm\s+guest\s+exec)\s+\d+")
_MAC_KEYS = ("hwaddr", "virtio", "e1000", "e1000e", "rtl8139", "vmxnet3")


def _fields(net0: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(net0 or "").strip().strip("'\"").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def net0_changes(current: str, proposed: str) -> list[str]:
    """The keys of the CURRENT net0 that the proposed one drops or changes, `firewall` aside."""
    cur, new = _fields(current), _fields(proposed)
    bad = []
    for k, v in cur.items():
        if k == "firewall":
            continue
        nv = new.get(k)
        if nv is None or (nv.lower() != v.lower() if k in _MAC_KEYS else nv != v):
            bad.append(k)
    # a MAC key swapped for another model's key still loses the address
    return bad


def _printf_lines(cmd: str) -> Optional[list[str]]:
    """The lines a `printf '%s\\n' 'a' 'b' … | tee FILE` writes, or None when they cannot be read off it."""
    head = cmd.split("|", 1)[0]
    if "printf" not in head:
        return None
    try:
        toks = shlex.split(head)
    except ValueError:
        return None
    if not toks or toks[0] != "printf" or len(toks) < 2:
        return None
    return [t for t in toks[2:]]


def lost_lines(existing: str, new_lines: list[str]) -> list[str]:
    """Lines of `existing` the new content neither keeps nor EDITS. A line one of the new lines extends or
    shortens is an edit (`IN ACCEPT -p tcp -dport 80 -source 192.168.1.0/24` → `IN ACCEPT -p tcp -dport 80`)."""
    keep = {" ".join(x.split()) for x in new_lines}
    longish = [k for k in keep if len(k) >= 12]
    out = []
    for ln in (existing or "").splitlines():
        n = " ".join(ln.split())
        if not n or n in keep:
            continue
        if any(n.startswith(k) or k.startswith(n) for k in longish):
            continue
        out.append(ln.strip())
    return out


async def destructive_rewrites(commands: list[str], spec, files: Optional[list[dict]] = None) -> list[dict]:
    """``[{command, why}]`` for every rewrite that would lose what the machine holds now."""
    if spec is None:
        return []
    from app.modules.service_truth import _probe
    out: list[dict] = []
    for c in commands or []:
        cmd = str(c)
        for m in _NET0_SET_RE.finditer(cmd):
            tool, gid, proposed = m.group(1), m.group(2), m.group(3)
            ok, conf = await _probe(spec, f"{tool} config {gid}")
            if not ok:
                continue
            cur = next((ln.split(":", 1)[1].strip() for ln in (conf or "").splitlines()
                        if ln.startswith("net0:")), "")
            if not cur:
                continue
            bad = net0_changes(cur, proposed)
            if bad:
                out.append({"command": cmd, "why": (
                    f"this rewrites guest {gid}'s net0 and drops or changes {', '.join(bad)} — `firewall` "
                    f"{NET0_MARKER}. Guest {gid}'s net0 now is `{cur}`: write that exact value with only "
                    f"`firewall=` changed (a dropped `ip`/`gw` puts the guest on DHCP, a dropped or changed MAC "
                    f"gives it a new address and loses its router reservation).")})
        if _IN_GUEST_RE.search(cmd):
            continue                    # a file inside a guest: the host cannot read it the same way
        targets = [m.group(2) for m in _TEE_RE.finditer(cmd)] + [m.group(1) for m in _REDIRECT_RE.finditer(cmd)]
        for path in dict.fromkeys(targets):
            if path.startswith(("/tmp/", "/dev/", "/proc/")):
                continue
            new = _printf_lines(cmd)
            if new is None:
                continue
            ok, existing = await _probe(spec, f"cat {path}")
            if not ok or not (existing or "").strip():
                continue
            lost = lost_lines(existing, new)
            if lost:
                out.append({"command": cmd, "why": (
                    f"this overwrites {path} and {len(lost)} {FILE_MARKER} — e.g. `{lost[0][:120]}`. Change "
                    f"only the lines the step is about: edit them in place, or write the whole file carrying "
                    f"every line it keeps.")})
    for f in files or []:
        path, content = str((f or {}).get("path") or ""), str((f or {}).get("content") or "")
        if not path.startswith("/") or path.startswith(("/tmp/", "/dev/", "/proc/")):
            continue
        ok, existing = await _probe(spec, f"cat {path}")
        if not ok or not (existing or "").strip():
            continue
        lost = lost_lines(existing, content.splitlines())
        if lost:
            out.append({"command": f"(write {path})", "why": (
                f"writing {path} replaces it, and {len(lost)} {FILE_MARKER} — e.g. `{lost[0][:120]}`. "
                f"Write the whole file carrying every line it keeps.")})
    if out:
        logger.warning("rewrite_guard_refused n=%d first=%r", len(out), out[0]["command"][:120])
    return out
