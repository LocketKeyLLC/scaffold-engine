r"""§17.1690 — a guest's own tools are measured, and a block that calls one the guest does not have is refused.

Live (job e5e22b92 T2 "Read full Sonarr queue", 2026-10-20): the in-guest read was `curl …/api/v3/queue … | jq -r …`
inside CT 104 -- which has no jq (`pct exec 104 -- which jq python3 curl wget` prints python3, curl and wget only). The
same guest had already cost job c2518102 T2 a round the day before: the read printed nothing, the step was recorded
done, and §17.1680b was written to judge it. Nothing told the drafter, and nothing refused the draft: the engine never
asked the guest which tools it has.

The pause now asks once (`which …`, a read the runner takes) and this module judges only the GUEST side of a block --
the body of the in-guest script (`<<'REMOTE'`) and the argument of `pct exec N -- sh|bash -c '…'` -- since a pipe
outside the `pct exec` runs on the host. A tool the block itself installs, or guards with `command -v`, is not judged.
"""
from __future__ import annotations

import re
import shlex
from typing import Optional

#: the tools a drafted read leans on; python3 is the remedy when jq is missing
PROBED = ("jq", "python3", "curl", "wget")
PROBE_ARGS = " ".join(PROBED)

_REMOTE_RE = re.compile(r"<<'REMOTE'\n(?P<body>.*?)\nREMOTE\n", re.S)
_WRAPPED_RE = re.compile(r"\bpct\s+exec\s+(?P<gid>\d{3,5})\s+--\s+(?:bash|sh)\s+-c\s+(?P<arg>'(?:[^']|'\"'\"')*'|\"(?:[^\"\\]|\\.)*\")")


def parse_which(output: str, ok: Optional[bool]) -> Optional[dict]:
    """``{tool: present}`` from `which <PROBED>` -- or None when the answer says nothing.

    `which` exits 1 when any tool is missing, so a non-zero exit is still an answer as long as it printed a path; an
    empty answer (the probe failed, the guest has no `which`) is blindness, never "every tool missing"."""
    if ok is None:
        return None
    paths = [ln.strip() for ln in str(output or "").splitlines() if ln.strip().startswith("/")]
    if not paths:
        return None
    names = {p.rsplit("/", 1)[-1] for p in paths}
    return {t: t in names for t in PROBED}


def guest_side(commands: list[str], files: Optional[list[dict]], gid: str) -> list[str]:
    """The texts that run INSIDE guest `gid`: in-guest script bodies, and `pct exec gid -- sh -c '<arg>'` arguments."""
    out: list[str] = []
    for f in files or []:
        content = str((f or {}).get("content") or "")
        path = str((f or {}).get("path") or "")
        bodies = [m.group("body") for m in _REMOTE_RE.finditer(content)]
        if bodies and re.search(rf"\bin_(?:ct|vm)_{gid}\b", path + content[:400]):
            out.extend(bodies)
    for c in commands or []:
        for m in _WRAPPED_RE.finditer(str(c)):
            if m.group("gid") != gid:
                continue
            try:
                parts = shlex.split(m.group("arg"))
            except ValueError:
                continue
            if parts:
                out.append(parts[0])
    return out


def calls_a_missing_tool(commands: list[str], files: Optional[list[dict]], gid: str,
                         tools: Optional[dict]) -> list[dict]:
    """``[{command, why}]`` for the first guest-side line that runs a tool guest `gid` measured as absent."""
    if not tools or not gid:
        return []
    missing = [t for t, present in tools.items() if present is False]
    if not missing:
        return []
    texts = guest_side(commands, files, gid)
    whole = "\n".join(texts)
    for tool in missing:
        if re.search(rf"\b(?:apt-get|apt|apk|dnf|yum)\b[^\n]*\binstall\b[^\n]*\b{re.escape(tool)}\b", whole) \
                or re.search(rf"\bcommand\s+-v\s+{re.escape(tool)}\b|\bwhich\s+{re.escape(tool)}\b", whole):
            continue
        use = re.compile(rf"(?:^|[|;&(]|\$\(|\bthen\b|\bdo\b|&&|\|\|)\s*{re.escape(tool)}(?![\w.-])")
        for t in texts:
            for ln in t.splitlines():
                if ln.lstrip().startswith("#") or not use.search(ln):
                    continue
                have = ", ".join(x for x, p in tools.items() if p) or "none of " + PROBE_ARGS
                remedy = (" Parse with python3, which it has: `… | python3 -c 'import sys,json; d=json.load(sys.stdin); …'`, "
                          "or a `python3 - <<'PY'` program that makes the call itself (urllib) and prints what the step needs."
                          if tools.get("python3") and tool == "jq" else
                          f" Use what it has ({have}), or report the missing tool as a finding.")
                return [{"command": ln.strip()[:200], "why": (
                    f"`{tool}` is not in guest {gid}: `which {PROBE_ARGS}` inside it, read just now, found {have} -- so "
                    f"this line fails with `{tool}: not found` and the read comes back empty (live, CT 104's Sonarr "
                    f"reads, twice).{remedy}")}]
    return []
