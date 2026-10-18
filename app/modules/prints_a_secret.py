r"""§17.1652 — a command that reads a service's credential must USE it, never print it.

Live (job 5db1b088, 2026-10-18), twice in one run: T6's check was `pct exec 102 -- bash -c "grep -oP
'(?<=<ApiKey>)[^<]+' /var/lib/prowlarr/config.xml"` -- Prowlarr's API key, printed into the run's output, the job's
record and the operator's frame -- and T9v2 drafted `sqlite3 … SELECT AccessToken FROM ApiKeys …` to print Jellyfin's
key. Both were refused only for other reasons. The engine's own reads put the key inside `$( … )` as a header value, so
it reaches the service and nothing else.

A credential read (an `<ApiKey>` extraction, a `SELECT AccessToken`, a `<Password>`) is refused when its output is the
command's own output: not inside a `$( … )` substitution.
"""
from __future__ import annotations

import re

_CREDENTIAL_READ_RE = re.compile(
    r"<ApiKey>|\bSELECT\s+AccessToken\b|<Password>|\bapi_?key\s*=\s*[\"']?\$?\(?\s*cat\b", re.I)


def _substitution_depth(cmd: str, at: int) -> int:
    """How many `$(` are open at `at` (single quotes inside a substitution are its own; we only count parens)."""
    depth, k = 0, 0
    while k < at and k < len(cmd):
        if cmd.startswith("$(", k):
            depth += 1
            k += 2
            continue
        if cmd[k] == ")" and depth:
            depth -= 1
        k += 1
    return depth


#: the shell readers that print what they read; a python program reads into a variable and is judged by what it prints
_READER_RE = re.compile(r"\b(grep|sed|awk|cut|cat|sqlite3|head|tail)\b(?P<flags>(?:\s+-[A-Za-z]+)*)")


def _prints_here(s: str, at: int) -> bool:
    """The credential read at `at` is a shell reader's output: a reader before it on the same pipeline segment, not a
    count/quiet grep, and not inside a `python3 -c` program."""
    head = s[:at]
    if re.search(r"\bpython3?\s+-c\b", head):
        return False
    seg = re.split(r"\|\||&&|;|\||\$\(|\bsh\s+-c\s+['\"]|\bbash\s+-c\s+['\"]", head)[-1]
    m = None
    for m in _READER_RE.finditer(seg):
        pass
    if m is None:
        return False
    if m.group(1) == "grep" and re.search(r"-[A-Za-z]*[cqlL]|--(?:count|quiet|files-with-matches)", m.group("flags") or ""):
        return False
    return True


def prints_a_credential(commands: list[str], verify: list[str]) -> list[dict]:
    out: list[dict] = []
    for c in list(commands or []) + list(verify or []):
        s = str(c)
        for m in _CREDENTIAL_READ_RE.finditer(s):
            if _substitution_depth(s, m.start()) == 0 and _prints_here(s, m.start()):
                out.append({"command": s[:200], "why": (
                    "this command READS a service's credential and PRINTS it: its output lands in the run's record, the "
                    "job and the operator's screen (live, T6's check printed Prowlarr's API key). Use the key where it is "
                    "read -- `-H \"X-Api-Key: $(sed -n 's:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p' <config.xml>)\"` -- or "
                    "check only that it is there (`grep -c '<ApiKey>' <config.xml>`) (§17.1652).")})
                break
    return out
