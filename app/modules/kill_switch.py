r"""§17.1670 — "configure the VPN kill-switch" for a container's WireGuard tunnel is the engine's own block.

Live (job 3cc24343 T8 "Configure nftables kill-switch", 2026-10-19; text: "Mirror CT 105 pattern: OUTPUT dropped except lo,
wg0, established/related, LAN 192.168.1.0/24, and WireGuard endpoint UDP port. Persist rules."): the draft pushed an
unseen nftables file, asked for the container's address as an input, and bundled T15's "add bitmagnet to Prowlarr" --
before bitmagnet existed (the engine refused that line). The shape is fixed and CT 105 runs it, measured:
iptables(-nft) OUTPUT policy DROP; accept lo, ct related/established, wg0, eth0 → the LAN, eth0 udp → the endpoint port;
IPv6 OUTPUT DROP with lo, established, wg0, fe80::/10; iptables-persistent (rules.v4/rules.v6).

One thing CT 105 does not have: Docker. A container's traffic is FORWARDED, never through OUTPUT -- if wg0 goes, it
falls back to eth0's default route and leaves in the clear. So forwarded traffic may leave eth0 only for the LAN
(Docker's own DOCKER-USER hook, through a chain the engine owns). The rules are applied live without touching Docker's
chains, saved as the engine's own rules files for boot, and proven at once: a connection forced out eth0 must fail and
one through the tunnel must work. (T11's "bring wg0 down" test stays the operator's proof.)
Values are measured at run time: the endpoint port from /etc/wireguard/wgN.conf, the LAN from the step's text or the
container's eth0 link route.
"""
from __future__ import annotations

import re
import shlex
from collections import Counter
from typing import Optional

_TITLE_RE = re.compile(r"\bkill[\s-]?switch\b", re.I)
_IFACE_RE = re.compile(r"\b(wg\d{1,3})\b")
_LAN_RE = re.compile(r"\bLAN\s+(?P<lan>\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b")
_CT_RE = re.compile(r"(?P<pre>\b(?:mirror|like|as\s+in|same\s+as|copy|from)\s+)?\b(?:CT|container|LXC)\s+(?P<gid>\d{3,5})\b", re.I)
_PCT_RE = re.compile(r"\bpct\s+(?:exec|push)\s+(\d{3,5})\b")


def guarded(node: Optional[dict], commands: Optional[list[str]] = None) -> tuple[str, str, str]:
    """(guest id, wg interface, LAN cidr or "") the kill-switch protects -- or empty strings."""
    title = str((node or {}).get("title") or "")
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not _TITLE_RE.search(title) or re.match(r"^\s*(?:verify|check|confirm|test)\b", title, re.I):
        return "", "", ""
    named = [m.group("gid") for m in _CT_RE.finditer(text) if not m.group("pre")]   # "mirror CT 105" is the pattern
    drafted = Counter(m.group(1) for c in commands or [] for m in _PCT_RE.finditer(str(c)))
    gid = named[0] if len(set(named)) == 1 else (drafted.most_common(1)[0][0] if drafted else "")
    if not gid:
        return "", "", ""
    wg = _IFACE_RE.search(text)
    lan = _LAN_RE.search(text)
    return gid, (wg.group(1) if wg else "wg0"), (lan.group("lan") if lan else "")


def block(node: Optional[dict], commands: Optional[list[str]] = None) -> Optional[tuple[list[str], list[str], str]]:
    gid, wg, lan = guarded(node, commands)
    if not gid:
        return None
    conf = f"/etc/wireguard/{wg}.conf"
    lan_sh = (f'lan={lan}' if lan else
              'lan=$(ip -4 route show dev eth0 scope link | cut -d" " -f1 | head -n 1)')
    values = (f'ep=$(sed -n "s/^Endpoint *= *//p" {conf} | head -n 1); port=${{ep##*:}}; '
              f'case "$port" in ""|*[!0-9]*) echo "FAILED: no Endpoint port in {conf}"; exit 1;; esac; '
              f'{lan_sh}; [ -n "$lan" ] || {{ echo "FAILED: no LAN route on eth0"; exit 1; }}; ')
    pre = (f'ip link show {wg} >/dev/null 2>&1 || {{ echo "FAILED: {wg} is not up -- the kill-switch would cut the '
           f'container off"; exit 1; }}; ts=$(date +%Y%m%d%H%M%S); iptables-save > /root/iptables.pre-killswitch.$ts; '
           f'ip6tables-save > /root/ip6tables.pre-killswitch.$ts; echo "SAVED the current rules as '
           f'/root/ip*tables.pre-killswitch.$ts"')
    install = ("echo iptables-persistent iptables-persistent/autosave_v4 boolean false | debconf-set-selections; "
               "echo iptables-persistent iptables-persistent/autosave_v6 boolean false | debconf-set-selections; "
               "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends iptables "
               "iptables-persistent curl")
    def ensure(tool: str, chain: str, rule: str) -> str:
        return f"{tool} -C {chain} {rule} 2>/dev/null || {tool} -A {chain} {rule}; "
    # rules are ADDED when missing, never flushed (the engine refuses a firewall flush): a second run changes nothing,
    # and anything else in these chains stays
    v4 = (values +
          "iptables -P OUTPUT ACCEPT; "
          + ensure("iptables", "OUTPUT", "-o lo -j ACCEPT")
          + ensure("iptables", "OUTPUT", "-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT")
          + ensure("iptables", "OUTPUT", f"-o {wg} -j ACCEPT")
          + ensure("iptables", "OUTPUT", '-o eth0 -d "$lan" -j ACCEPT')
          + ensure("iptables", "OUTPUT", '-o eth0 -p udp --dport "$port" -j ACCEPT')
          # §17.1670d — Docker's own bridges are container networks INSIDE this machine, not a way out: live, with them
          # dropped the container host could not reach its own published port (docker-proxy → 172.x, curl 3333 → 000)
          + ensure("iptables", "OUTPUT", "-o docker0 -j ACCEPT")
          + ensure("iptables", "OUTPUT", "-o br-+ -j ACCEPT")
          + "iptables -P OUTPUT DROP; "
          "iptables -N SCAFFOLD-KS-FWD 2>/dev/null || true; "
          + ensure("iptables", "SCAFFOLD-KS-FWD", '-o eth0 -d "$lan" -j RETURN')
          + ensure("iptables", "SCAFFOLD-KS-FWD", "-o eth0 -j DROP")
          + "iptables -N DOCKER-USER 2>/dev/null || true; "
          "iptables -C DOCKER-USER -j SCAFFOLD-KS-FWD 2>/dev/null || iptables -I DOCKER-USER 1 -j SCAFFOLD-KS-FWD; "
          "iptables -C FORWARD -j DOCKER-USER 2>/dev/null || iptables -I FORWARD 1 -j DOCKER-USER; "
          f'echo "KILL-SWITCH v4: out only by lo, {wg}, the local Docker bridges, the LAN $lan and udp/$port to the '
          'endpoint; forwarded traffic leaves eth0 only for the LAN"')
    v6 = ("ip6tables -P OUTPUT ACCEPT; "
          + ensure("ip6tables", "OUTPUT", "-o lo -j ACCEPT")
          + ensure("ip6tables", "OUTPUT", "-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT")
          + ensure("ip6tables", "OUTPUT", f"-o {wg} -j ACCEPT")
          + ensure("ip6tables", "OUTPUT", "-o eth0 -d fe80::/10 -j ACCEPT")
          + ensure("ip6tables", "OUTPUT", "-o docker0 -j ACCEPT")
          + ensure("ip6tables", "OUTPUT", "-o br-+ -j ACCEPT")
          + "ip6tables -P OUTPUT DROP; ip6tables -P FORWARD DROP; "
          f'echo "KILL-SWITCH v6: out only by lo, {wg} and link-local"')
    # the boot copy is the engine's own rules (Docker writes its chains itself when it starts); a live restore would
    # flush Docker's chains, so the live rules were applied above rule by rule
    def lines(*ls: str) -> str:
        return " ".join('"' + ln + '"' for ln in ls)
    save = (values + "mkdir -p /etc/iptables; umask 022; printf \"%s\\n\" "
            + lines("*filter", ":INPUT ACCEPT [0:0]", ":FORWARD ACCEPT [0:0]", ":OUTPUT DROP [0:0]",
                    ":DOCKER-USER - [0:0]", ":SCAFFOLD-KS-FWD - [0:0]", "-A OUTPUT -o lo -j ACCEPT",
                    "-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT", f"-A OUTPUT -o {wg} -j ACCEPT",
                    "-A OUTPUT -o eth0 -d $lan -j ACCEPT", "-A OUTPUT -o eth0 -p udp --dport $port -j ACCEPT",
                    "-A OUTPUT -o docker0 -j ACCEPT", "-A OUTPUT -o br-+ -j ACCEPT",
                    "-A DOCKER-USER -j SCAFFOLD-KS-FWD", "-A SCAFFOLD-KS-FWD -o eth0 -d $lan -j RETURN",
                    "-A SCAFFOLD-KS-FWD -o eth0 -j DROP", "COMMIT")
            + " > /etc/iptables/rules.v4; printf \"%s\\n\" "
            + lines("*filter", ":INPUT ACCEPT [0:0]", ":FORWARD DROP [0:0]", ":OUTPUT DROP [0:0]",
                    "-A OUTPUT -o lo -j ACCEPT", "-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
                    f"-A OUTPUT -o {wg} -j ACCEPT", "-A OUTPUT -o eth0 -d fe80::/10 -j ACCEPT",
                    "-A OUTPUT -o docker0 -j ACCEPT", "-A OUTPUT -o br-+ -j ACCEPT", "COMMIT")
            + " > /etc/iptables/rules.v6; iptables-restore --test < /etc/iptables/rules.v4 && "
            "ip6tables-restore --test < /etc/iptables/rules.v6 && "
            "echo \"SAVED for boot: /etc/iptables/rules.v4 and rules.v6 (they parse)\"")
    prove = ('if curl -s -m 6 --interface eth0 -o /dev/null https://1.1.1.1/; then echo "FAILED: traffic still leaves '
             'eth0 outside the tunnel"; exit 1; fi; echo "BLOCKED: nothing reaches the internet by eth0"; '
             'code=$(curl -s -m 20 -o /dev/null -w "%{http_code}" https://airvpn.org/); case "$code" in 2*|3*) '
             'echo "THROUGH THE TUNNEL: airvpn.org answered $code";; *) echo "FAILED: the tunnel does not reach the '
             'internet (HTTP $code)"; exit 1;; esac')
    q = shlex.quote
    cmds = [f"pct exec {gid} -- sh -c {q(pre)}",
            f"pct exec {gid} -- sh -c {q(install)}",
            f"pct exec {gid} -- sh -c {q(v4)}",
            f"pct exec {gid} -- sh -c {q(v6)}",
            f"pct exec {gid} -- sh -c {q(save)}",
            f"pct exec {gid} -- systemctl enable netfilter-persistent",
            f"pct exec {gid} -- sh -c {q(prove)}"]
    # §17.1670b — the exact chains, not `nft list ruleset`: live, Docker's chains made it long and its first table is the
    # empty default `inet filter … policy accept`; the judge read that and failed a step whose switch was live and proven
    verify = [f"pct exec {gid} -- iptables -S OUTPUT", f"pct exec {gid} -- iptables -S SCAFFOLD-KS-FWD",
              f"pct exec {gid} -- ip6tables -S OUTPUT"]
    note = (f"built the kill-switch for {wg} in container {gid} as the engine's own block (§17.1670), mirroring CT 105 "
            f"as measured: OUTPUT drops all but lo, established, {wg}, the LAN ({lan or 'from eth0'}) and udp to the "
            "endpoint port (read from the tunnel's config); IPv6 out only by lo, the tunnel and link-local. Docker's "
            "forwarded traffic -- which never passes OUTPUT -- may leave eth0 only for the LAN (DOCKER-USER). The "
            "current rules are saved first, the boot copy is written and parse-tested, and the switch is proven at "
            "once: a connection forced out eth0 must fail, one through the tunnel must work.")
    # §17.1570g — what the checks SHOW when the switch is whole, so "already met" is judged against the full rule set
    # (live: a switch from before §17.1670d was judged met, and the Docker-bridge rule never went in)
    expect = {verify[0]: (f"-P OUTPUT DROP, and ACCEPT rules for -o lo, RELATED,ESTABLISHED, -o {wg}, -o eth0 to the "
                          "LAN, -o eth0 udp to the endpoint port, -o docker0 and -o br-+"),
              verify[1]: "-A SCAFFOLD-KS-FWD ... -o eth0 -j RETURN for the LAN, then -A SCAFFOLD-KS-FWD -o eth0 -j DROP",
              verify[2]: f"-P OUTPUT DROP, and ACCEPT rules for -o lo, RELATED,ESTABLISHED, -o {wg}, fe80::/10, -o docker0 "
                         "and -o br-+"}
    return cmds, verify, note, expect


#: §17.1670c — "verify the kill-switch blocks traffic (bring wgN down …)"
_PROOF_TITLE_RE = re.compile(r"^\s*(?:verify|test|check|confirm|prove)\b[^\n]*\bkill[\s-]?switch\b", re.I)


def proof_block(node: Optional[dict], commands: Optional[list[str]] = None) -> Optional[tuple[list[str], list[str], str]]:
    """Live (job 3cc24343 T11): the draft was `wg-quick down wg0` / `curl http://1.1.1.1` / `wg-quick up wg0` -- the curl
    is MEANT to fail, a failing command stops the block, so the tunnel would have been left down and the step recorded
    failed while the switch worked. One command: stop the unit, try to get out by IP (no DNS in the way), ALWAYS start it
    again and wait for a handshake, then say what happened -- pass only when the attempt was blocked and the tunnel is back."""
    title = str((node or {}).get("title") or "")
    if not _PROOF_TITLE_RE.match(title):
        return None
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    named = [m.group("gid") for m in _CT_RE.finditer(text) if not m.group("pre")]
    drafted = Counter(m.group(1) for c in commands or [] for m in _PCT_RE.finditer(str(c)))
    gid = named[0] if len(set(named)) == 1 else (drafted.most_common(1)[0][0] if drafted else "")
    if not gid:
        return None
    wg = (_IFACE_RE.search(text) or _IFACE_RE.search(" ".join(commands or [])))
    wg = wg.group(1) if wg else "wg0"
    script = (f'systemctl stop wg-quick@{wg}; '
              f'if ip link show {wg} >/dev/null 2>&1; then r="NOT DOWN"; '
              'elif curl -s -m 8 -o /dev/null https://1.1.1.1/ || curl -s -m 8 -o /dev/null http://9.9.9.9/; then r=LEAK; '
              'else r=BLOCKED; fi; '
              f'systemctl start wg-quick@{wg}; up=""; '
              f'for i in $(seq 1 30); do h=$(wg show {wg} latest-handshakes 2>/dev/null | cut -f2 | head -n 1); '
              'if [ "${h:-0}" -gt 0 ] 2>/dev/null; then up=yes; break; fi; sleep 1; done; '
              f'case "$r" in BLOCKED) echo "BLOCKED: with {wg} down nothing reached the internet";; '
              f'LEAK) echo "FAILED: with {wg} down traffic got out -- the kill-switch does not hold";; '
              f'*) echo "FAILED: {wg} did not go down, so nothing was tested";; esac; '
              f'if [ -n "$up" ]; then echo "BACK UP: {wg} handshook with its peer again"; '
              f'else echo "FAILED: {wg} did not come back (no handshake in 30 s)"; fi; '
              '[ "$r" = BLOCKED ] && [ -n "$up" ]')
    cmds = [f"pct exec {gid} -- sh -c {shlex.quote(script)}"]
    verify = [f"pct exec {gid} -- ip -4 addr show {wg}", f"pct exec {gid} -- iptables -S OUTPUT"]
    note = (f"proved the kill-switch in container {gid} as one engine command (§17.1670c): wg-quick@{wg} is stopped, the "
            "container tries to reach two public addresses by IP, the tunnel is ALWAYS started again and must handshake, "
            "and only then is the result read -- the drafted `curl` was meant to fail, which would have stopped the block "
            "and left the tunnel down.")
    return cmds, verify, note
