r"""§17.1669 — "set up WireGuard wgN from <config file>" in a container is the engine's own block.

Live (job 3cc24343 T7 "Configure WireGuard wg0", 2026-10-19; text: "Install wireguard-tools, set up wg0 from
/root/airvpn-bitmagnet.conf. Enable wg-quick@wg0."): the first draft installed wireguard-tools alone (wg-quick then
fails on AirVPN's `DNS =` line without resolvconf, §17.1668); its redraft dropped the copy to /etc/wireguard/wg0.conf --
the only path `wg-quick@wg0` reads -- so the start would have failed "wg0.conf does not exist". Two drafts, two
different ways not to bring the tunnel up.

The shape is fixed, so the engine writes it: require the source file in the guest; install wireguard-tools and
resolvconf; keep any existing /etc/wireguard/wgN.conf as a dated .bak and install the source there, mode 600, root;
enable and (re)start wg-quick@wgN; then wait for a real HANDSHAKE with the peer -- an interface that is up but never
spoke to the server is not a tunnel. Only a container step whose title leads with configure/set up/bring up/enable
… WireGuard|wgN and whose text names `wgN` and `from <path>` is claimed; the guest is the one the text names, else the
single guest every drafted `pct exec` targets.
"""
from __future__ import annotations

import re
import shlex
from typing import Optional

_TITLE_RE = re.compile(r"^\s*(?:[A-Za-z]+\s+\d{3,5}\s*:\s*)?(?:configure|set\s*up|setup|bring\s+up|enable|start)\b"
                       r"[^\n]{0,40}\b(?:wireguard|wg\d+)\b", re.I)
_IFACE_RE = re.compile(r"\b(wg\d{1,3})\b")
_FROM_RE = re.compile(r"\bfrom\s+`?(?P<src>/[\w.@+-]+(?:/[\w.@+-]+)*)")
_CT_RE = re.compile(r"\b(?:CT|container|LXC)\s+(?P<gid>\d{3,5})\b", re.I)
_PCT_RE = re.compile(r"\bpct\s+exec\s+(\d{3,5})\b")


def tunnel(node: Optional[dict], commands: Optional[list[str]] = None) -> tuple[str, str, str]:
    """(guest id, interface, source config path) -- or empty strings."""
    title = str((node or {}).get("title") or "")
    if not _TITLE_RE.match(title):
        return "", "", ""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    iface, src = _IFACE_RE.search(text), _FROM_RE.search(text)
    if not iface or not src:
        return "", "", ""
    ct = _CT_RE.search(text)
    gids = {m.group(1) for c in commands or [] for m in _PCT_RE.finditer(str(c))}
    gid = ct.group("gid") if ct else (next(iter(gids)) if len(gids) == 1 else "")
    return (gid, iface.group(1), src.group("src").rstrip(".,;:")) if gid else ("", "", "")


def block(node: Optional[dict], commands: Optional[list[str]] = None) -> Optional[tuple[list[str], list[str], str]]:
    gid, wg, src = tunnel(node, commands)
    if not gid:
        return None
    dst = f"/etc/wireguard/{wg}.conf"
    s = shlex.quote(src)
    place = (f"test -s {s} || {{ echo \"FAILED: {src} is not in container {gid}\"; exit 1; }}; "
             f"mkdir -p /etc/wireguard && chmod 700 /etc/wireguard; "
             f"if [ -f {dst} ]; then cp -a {dst} {dst}.bak.$(date +%Y%m%d%H%M%S); fi; "
             f"install -m 600 -o root -g root {s} {dst} && echo PLACED {dst}")
    present = f'test -s {s} || {{ echo "FAILED: {src} is not in container {gid}"; exit 1; }}; echo SOURCE PRESENT {src}'
    handshake = (f"for i in $(seq 1 30); do h=$(wg show {wg} latest-handshakes 2>/dev/null | awk '{{print $2}}' | head -n 1); "
                 f"if [ \"${{h:-0}}\" -gt 0 ] 2>/dev/null; then echo \"HANDSHAKE: {wg} is talking to its peer\"; exit 0; fi; "
                 f"sleep 1; done; echo \"FAILED: {wg} is up but never completed a handshake with its peer in 30 s\"; "
                 f"systemctl --no-pager status wg-quick@{wg} | tail -n 15; exit 1")
    # §17.1669b — Debian's resolvconf turns /etc/resolv.conf into a symlink to /run/resolvconf/resolv.conf, which in a
    # Proxmox container nothing feeds: live, CT 109 lost DNS the moment it was installed and `wg setconf` retried the
    # Endpoint's hostname forever (the restart hung past the runner's 180 s). Proxmox writes a plain resolv.conf at
    # container start (CT 105 has one, resolvconf installed and inert); the engine writes the same file now, from the
    # container's own `pct config` nameservers (the host's, when it has none).
    restore = (f"if pct exec {gid} -- test -L /etc/resolv.conf; then "
               f"ns=$(pct config {gid} | grep '^nameserver:' | cut -d' ' -f2-); sd=$(pct config {gid} | grep '^searchdomain:' | cut -d' ' -f2-); "
               f"{{ echo '# --- BEGIN PVE ---'; if [ -n \"$sd\" ]; then echo \"search $sd\"; fi; "
               f"if [ -n \"$ns\" ]; then for x in $ns; do echo \"nameserver $x\"; done; else grep '^nameserver' /etc/resolv.conf; fi; "
               f"echo '# --- END PVE ---'; }} > /tmp/scaffold-resolv.{gid} && pct exec {gid} -- rm -f /etc/resolv.conf && "
               f"pct push {gid} /tmp/scaffold-resolv.{gid} /etc/resolv.conf --perms 644 && rm -f /tmp/scaffold-resolv.{gid} && "
               f"echo \"RESTORED: container {gid}'s resolv.conf from its Proxmox nameservers\"; "
               f"else echo \"resolv.conf in {gid} is a plain file\"; fi")
    # the Endpoint must resolve BEFORE the start: `wg setconf` retries an unresolvable name without end
    resolve = (f"ep=$(sed -n \"s/^Endpoint *= *//p\" {s} | head -n 1); h=${{ep%:*}}; "
               f"[ -n \"$h\" ] || {{ echo \"FAILED: no Endpoint in {src}\"; exit 1; }}; "
               f"case \"$h\" in \\[*|*[!0-9.]*) case \"$h\" in \\[*) ;; *) getent hosts \"$h\" >/dev/null || "
               f"{{ echo \"FAILED: container {gid} cannot resolve $h -- wg-quick would wait on it forever\"; exit 1; }};; esac;; esac; "
               f"echo \"ENDPOINT RESOLVES: $h\"")
    install = ("DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
               "apt-get install -y -qq --no-install-recommends wireguard-tools resolvconf")   # recommends pull a kernel
    cmds = [
        f"pct exec {gid} -- sh -c {shlex.quote(present)}",
        f"pct exec {gid} -- sh -c {shlex.quote(install)}",
        restore,
        f"pct exec {gid} -- sh -c {shlex.quote(place)}",
        f"pct exec {gid} -- sh -c {shlex.quote(resolve)}",
        f"pct exec {gid} -- systemctl enable wg-quick@{wg}",
        f"pct exec {gid} -- timeout 120 systemctl restart wg-quick@{wg}",
        f"pct exec {gid} -- sh -c {shlex.quote(handshake)}",
    ]
    # the runner's read channel takes neither `systemctl` nor `wg`: the handshake is proved by the block's last command
    verify = [f"pct exec {gid} -- ip -4 addr show {wg}", f"pct exec {gid} -- ip -4 rule show"]
    note = (f"set up {wg} in container {gid} from {src} as the engine's own block (§17.1669): the source must be there; "
            f"wireguard-tools and resolvconf (a provider's `DNS =` line needs it) are installed without recommends; if "
            f"that turned /etc/resolv.conf into resolvconf's (empty) symlink, the container's Proxmox nameservers are "
            f"written back as a plain file; any {dst} is kept as a dated .bak and {src} is installed there mode 600; the "
            f"Endpoint must resolve first; wg-quick@{wg} is enabled and restarted (120 s limit), and the step waits for "
            "a real handshake with the peer -- an interface that never spoke to the server is not a tunnel.")
    return cmds, verify, note
