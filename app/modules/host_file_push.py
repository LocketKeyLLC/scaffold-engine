r"""§17.1667 — "copy <host file> into container N" is the engine's own `pct push`, never an in-guest draft.

Live (job 3cc24343 T6 "Copy AirVPN config", 2026-10-19): the step said "Copy the operator-provided config from host
/root/airvpn-bitmagnet.conf into CT 109 /root/airvpn-bitmagnet.conf, chmod 600". The draft went through the
`run_in_container` template, so every line ran INSIDE CT 109 -- where the host's file does not exist -- and the model
filled the gap with `cat > /root/airvpn-bitmagnet.conf <<'EOF' <CONFIG_CONTENT> EOF`: a placeholder asking the
operator to paste a WireGuard private key into the decide input, to be written over the very file the step copies.

The engine can do this from the host, where both ends are in reach: check the file is there, keep a copy of any file
already at the target in the guest, `pct push` with the asked mode, then compare checksums (never the contents).
Only a step whose TITLE leads with copy/push/transfer and whose text names a host path and a container id is claimed.
"""
from __future__ import annotations

import re
import shlex
from typing import Optional

_TITLE_RE = re.compile(r"^\s*(?:[A-Za-z]+\s+\d{3,5}\s*:\s*)?(?:copy|push|transfer)\b", re.I)
_PATH = r"/[\w.@+-]+(?:/[\w.@+-]+)*"
_FROM_TO_RE = re.compile(
    rf"\bfrom\s+(?:the\s+)?(?:proxmox\s+)?host(?:'s)?\s+(?:file\s+)?(?P<src>{_PATH})\s+(?:in)?to\s+(?:the\s+)?"
    rf"(?:CT|container|LXC)\s+(?P<gid>\d{{3,5}})(?:'s)?(?:\s*(?::|at|as)?\s*(?P<dst>{_PATH}))?", re.I)
_MODE_RE = re.compile(r"\bchmod\s+(?P<mode>[0-7]{3,4})\b")


def _clean(p: str) -> str:
    return p.rstrip(".") if p else p


def pushed_file(node: Optional[dict]) -> tuple[str, str, str, str]:
    """(guest id, host source, guest target, mode or "") the step copies -- or empty strings."""
    title = str((node or {}).get("title") or "")
    if not _TITLE_RE.match(title):
        return "", "", "", ""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    m = _FROM_TO_RE.search(text)
    if not m:
        return "", "", "", ""
    src = _clean(m.group("src"))
    dst = _clean(m.group("dst") or "") or src
    mm = _MODE_RE.search(text)
    mode = mm.group("mode") if mm else ""
    return m.group("gid"), src, dst, mode


def block(node: Optional[dict]) -> Optional[tuple[list[str], list[str], str]]:
    gid, src, dst, mode = pushed_file(node)
    if not gid:
        return None
    s, d = shlex.quote(src), shlex.quote(dst)
    keep = (f"if [ -f {d} ]; then cp -a {d} {d}.bak.$(date +%Y%m%d%H%M%S); fi")
    cmds = [
        f'[ -f {s} ] || {{ echo "FAILED: {src} is not on the Proxmox host -- put it there first"; exit 1; }}; '
        f'pct status {gid} | grep -q running || {{ echo "FAILED: container {gid} is not running"; exit 1; }}; '
        f"echo HOST FILE PRESENT {src}",
        f"pct exec {gid} -- sh -c {shlex.quote(keep)}",
        f"pct push {gid} {s} {d}" + (f" --perms {mode}" if mode else ""),
        f'a=$(sha256sum {s} | cut -d" " -f1); b=$(pct exec {gid} -- sha256sum {d} | cut -d" " -f1); '
        f'if [ -n "$a" ] && [ "$a" = "$b" ]; then echo "COPIED: {dst} in {gid} has the same content as the host file"; '
        f'else echo "FAILED: {dst} in {gid} differs from the host file"; exit 1; fi',
    ]
    verify = [f"pct exec {gid} -- stat -c '%a %s %n' {d}"]
    note = (f"copied from the host, where both ends are in reach (§17.1667): {src} must already be on the Proxmox host; "
            f"any {dst} already in container {gid} is kept as a dated .bak, the file is pushed with `pct push`"
            + (f" (mode {mode})" if mode else "") + ", and the two copies are compared by checksum -- the contents "
            "are never printed or asked for.")
    return cmds, verify, note
