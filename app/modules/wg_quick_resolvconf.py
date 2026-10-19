r"""§17.1668 — a block that installs WireGuard and brings a tunnel up with `wg-quick` installs `resolvconf` too.

Live (job 3cc24343 T7 "Configure WireGuard wg0", 2026-10-19): `apt-get install -y wireguard-tools`, then
`systemctl start wg-quick@wg0` on AirVPN's config -- which carries `DNS = 10.128.0.1, …`. wg-quick applies a DNS line
through `resolvconf`, and Debian's wireguard-tools only *suggests* it, so the start would have failed with
"resolvconf: command not found" (CT 105, the same provider's config, runs with resolvconf installed). Without a DNS
line resolvconf is unused, so adding it is always safe.

The repair appends `resolvconf` to the install line that installs wireguard, when the same block runs `wg-quick` and
names neither resolvconf nor openresolv.
"""
from __future__ import annotations

import re

_INSTALL_RE = re.compile(r"(\bapt(?:-get)?\s+(?:-\S+\s+)*install\b[^\n;&|]*?\bwireguard(?:-tools)?\b)")
_WG_QUICK_RE = re.compile(r"\bwg-quick\b")
_HAS_RESOLVCONF_RE = re.compile(r"\b(?:resolvconf|openresolv)\b")


def repair(commands: list[str], verify: list[str], files: list[dict] | None) -> tuple[list[str], list[str], list[dict], list[dict]]:
    texts = [str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []]
    whole = "\n".join(texts + [str(v) for v in verify or []])
    if not _WG_QUICK_RE.search(whole) or _HAS_RESOLVCONF_RE.search(whole) or not _INSTALL_RE.search("\n".join(texts)):
        return list(commands or []), list(verify or []), list(files or []), []
    done = False

    def add(s: str) -> str:
        nonlocal done
        if done:
            return s
        new, n = _INSTALL_RE.subn(r"\1 resolvconf", s, count=1)
        done = bool(n)
        return new

    out_c = [add(str(c)) for c in commands or []]
    out_f = []
    for f in files or []:
        body = str((f or {}).get("content") or "")
        new = add(body)
        out_f.append({**f, "content": new} if new != body else f)
    notes = [{"why": ("the block brings a tunnel up with `wg-quick`, which applies a config's `DNS =` line through "
                      "`resolvconf`, and Debian's wireguard-tools does not install it (live, T7 would have failed "
                      "\"resolvconf: command not found\" on AirVPN's config); added `resolvconf` to the install "
                      "(§17.1668).")}] if done else []
    return out_c, list(verify or []), out_f, notes
