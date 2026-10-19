r"""§17.1667 — "copy <host file> into container N" is the engine's own `pct push`, never an in-guest draft.

Live (job 3cc24343 T6 "Copy AirVPN config", 2026-10-19): the step said "Copy the operator-provided config from host
/root/airvpn-bitmagnet.conf into CT 109 /root/airvpn-bitmagnet.conf, chmod 600". The draft went through the
`run_in_container` template, so every line ran INSIDE CT 109 -- where the host's file does not exist -- and the model
filled the gap with `cat > /root/airvpn-bitmagnet.conf <<'EOF' <CONFIG_CONTENT> EOF`: a placeholder asking the
operator to paste a WireGuard private key into the decide input, to be written over the very file the step copies.

The engine can do this from the host, where both ends are in reach: check the file is there, keep a copy of any file
already at the target in the guest, `pct push` with the asked mode, then compare checksums (never the contents).
Only a step whose TITLE leads with copy/push/transfer and whose text names a host path and a container id is claimed.

The same T6, pointed at the config the engine HOLDS (`$AIRVPN_BITMAGNET_CONF`, stored once from the operator's file):
the draft became `tee /root/airvpn-bitmagnet.conf <<'EOF'` / `EOF` -- the reference gone, the heredoc empty, so the run
would have emptied the file (and `tee` would have printed it). A script pushed INTO a guest never sees a held secret;
the runner expands it in the host-side command (§17.1191). So "write the held secret $NAME into container N at PATH"
is the engine's own block too: `printf '%s' "<NAME>" | pct exec N -- sh -c 'umask 077; cat > PATH.new …; mv …'`
(printf is a builtin: the value is in no argv and no log), then the two checksums compared. Only a name some store
already holds is claimed.
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
_MODE_RE = re.compile(r"\b(?:chmod|mode)\s+(?P<mode>[0-7]{3,4})\b")
_SECRET_TITLE_RE = re.compile(r"^\s*(?:[A-Za-z]+\s+\d{3,5}\s*:\s*)?(?:write|copy|put|place|push|install|transfer)\b", re.I)
_SECRET_REF_RE = re.compile(r"(?:\$\{?|<)(?P<name>[A-Z][A-Z0-9_]{2,63})\b")
_INTO_RE = re.compile(rf"\binto\s+(?:the\s+)?(?:CT|container|LXC)\s+(?P<gid>\d{{3,5}})(?:'s)?\s*(?::|at|as)?\s*(?P<dst>{_PATH})",
                      re.I)


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


def written_secret(node: Optional[dict], held: Optional[list] = None) -> tuple[str, str, str, str]:
    """(guest id, held secret name, guest target, mode) the step writes -- or empty strings."""
    title = str((node or {}).get("title") or "")
    if not _SECRET_TITLE_RE.match(title) or not held:
        return "", "", "", ""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    names = [m.group("name") for m in _SECRET_REF_RE.finditer(text) if m.group("name") in {str(h) for h in held}]
    m = _INTO_RE.search(text)
    if len(set(names)) != 1 or not m:
        return "", "", "", ""
    mm = _MODE_RE.search(text)
    return m.group("gid"), names[0], _clean(m.group("dst")), (mm.group("mode") if mm else "600")


def secret_block(node: Optional[dict], held: Optional[list] = None) -> Optional[tuple[list[str], list[str], str]]:
    gid, name, dst, mode = written_secret(node, held)
    if not gid:
        return None
    d = shlex.quote(dst)
    new = shlex.quote(dst + ".scaffold-new")
    keep = f"if [ -f {d} ]; then cp -a {d} {d}.bak.$(date +%Y%m%d%H%M%S); fi"
    write = f"umask 077; cat > {new} && chmod {mode} {new} && mv -f {new} {d}"
    cmds = [
        f'pct status {gid} | grep -q running || {{ echo "FAILED: container {gid} is not running"; exit 1; }}; '
        f"echo CONTAINER {gid} RUNNING",
        f"pct exec {gid} -- sh -c {shlex.quote(keep)}",
        f'printf \'%s\' "<{name}>" | pct exec {gid} -- sh -c {shlex.quote(write)}',
        f'a=$(printf \'%s\' "<{name}>" | sha256sum | cut -d" " -f1); b=$(pct exec {gid} -- sha256sum {d} | cut -d" " -f1); '
        f'if [ -n "$b" ] && [ "$a" = "$b" ]; then echo "WRITTEN: {dst} in {gid} holds the stored {name}"; '
        f'else echo "FAILED: {dst} in {gid} does not match the stored {name}"; exit 1; fi',
    ]
    verify = [f"pct exec {gid} -- stat -c '%a %U %s %n' {d}"]
    note = (f"wrote the held secret {name} into container {gid} from the host (§17.1667): a script pushed into the "
            f"guest never sees a held secret, so the runner expands it in a host-side `printf` piped into the guest "
            f"(in no argv, no log); any {dst} already there is kept as a dated .bak, the file is written with mode "
            f"{mode} and swapped in whole, and the stored value and the file are compared by checksum -- the "
            "contents are never printed.")
    return cmds, verify, note
