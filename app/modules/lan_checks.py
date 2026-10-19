r"""§17.1679 — a check the step says to make FROM THE LAN runs on the host, not inside the guest it checks.

Live (job 3cc24343 T14 "Verify bitmagnet web UI", 2026-10-19; text: "curl http://192.168.1.29:3333 from LAN. Confirm HTTP
200."): the drafted check was `pct exec 109 -- bash -c "curl … http://192.168.1.29:3333"` -- CT 109 calling its own
address, which proves nothing about the LAN (the guest's own firewall, the bridge, the kill-switch's OUTPUT rules on the
reply path are all skipped). The step was settled by hand from the Proxmox host and the operator's laptop.

The Proxmox host is a LAN machine the runner already reaches, so in a step whose text says "from (the) LAN" a
`pct exec N -- bash|sh -c "<curl … http://<private LAN address>…>"` line is unwrapped to run on the host. A loopback
call, a non-curl line, or a step that does not say "from the LAN" is left as drafted.
"""
from __future__ import annotations

import re
import shlex
from typing import Optional

_FROM_LAN_RE = re.compile(r"\bfrom\s+(?:the\s+)?(?:LAN|local\s+network|another\s+machine)\b", re.I)
_WRAPPED_RE = re.compile(r"^\s*pct\s+exec\s+(?P<gid>\d{3,5})\s+--\s+(?:bash|sh)\s+-c\s+(?P<arg>.+?)\s*$", re.S)
_LAN_URL_RE = re.compile(r"https?://(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
                         r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?::\d+)?")


def _unwrap(line: str) -> Optional[str]:
    m = _WRAPPED_RE.match(str(line))
    if not m:
        return None
    try:
        parts = shlex.split(m.group("arg"))
    except ValueError:
        return None
    if len(parts) != 1:
        return None
    inner = parts[0].strip()
    if not re.match(r"curl\b", inner) or not _LAN_URL_RE.search(inner) or re.search(r"[;&|`]|\$\(", inner):
        return None
    return inner


def from_the_host(node: Optional[dict], commands: list[str], verify: list[str]) -> tuple[list[str], list[str], list[dict]]:
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not _FROM_LAN_RE.search(text):
        return list(commands or []), list(verify or []), []
    moved: list[str] = []

    def fix(lines: list[str]) -> list[str]:
        out = []
        for ln in lines or []:
            inner = _unwrap(ln)
            if inner:
                moved.append(inner)
                out.append(inner)
            else:
                out.append(str(ln))
        return out

    c, v = fix(commands), fix(verify)
    notes = [{"why": ("the step says to check FROM THE LAN, and the check ran inside the guest it checks -- a guest "
                      "calling its own address proves nothing about the LAN (live, T14 curled CT 109 from CT 109); "
                      f"ran it from the Proxmox host, a LAN machine: {', '.join(f'`{x[:80]}`' for x in moved[:3])} "
                      "(§17.1679).")}] if moved else []
    return c, v, notes
