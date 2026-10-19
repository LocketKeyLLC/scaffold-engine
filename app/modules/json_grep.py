r"""§17.1658 — a grep for a JSON key/value literal matches it with or without the space after the colon.

Live, twice in job 5db1b088 (2026-10-18): T5 counted Prowlarr's enabled indexers with `grep -c '"enable": true'` on
JSON that came back compact (`"enable":true`) -- 0, "no indexers enabled", while 45 were; T15 looked for
`'"status":"importBlocked"'` in Sonarr's pretty-printed JSON (`"status": "completed"`) -- 0 blocked, PASS, while 82 were.
Whether an API answers `"k": v` or `"k":v` is the server's formatting, not the data.

A grep whose whole pattern is ONE JSON key/value literal (single-quoted `'"k": "v"'`, or double-quoted with escaped
quotes `"\"k\":\"v\""`) becomes an extended regex that allows any spaces after the colon. Anything else -- other
patterns, `-P`, `-F` with more than the literal -- is left as drafted.
"""
from __future__ import annotations

import re

#: grep, its flags, and a single-quoted pattern that is exactly `"key": value`
_SQ_RE = re.compile(r"""(?P<head>\bgrep\b(?P<flags>(?:\s+-[A-Za-z]+)*)\s+)'(?P<pat>"[A-Za-z_][\w.-]*"\s*:\s*(?:"[^"'\\]*"|true|false|null|-?\d+(?:\.\d+)?))'""")
#: the same inside double quotes: `"\"key\": \"value\""` / `"\"key\":true"`
_DQ_RE = re.compile(r"""(?P<head>\bgrep\b(?P<flags>(?:\s+-[A-Za-z]+)*)\s+)"(?P<pat>\\"[A-Za-z_][\w.-]*\\"\s*:\s*(?:\\"[^"'\\]*\\"|true|false|null|-?\d+(?:\.\d+)?))"(?!\w)""")


def _flags(flags: str) -> str | None:
    words = [w for w in (flags or "").split() if w]
    if any("P" in w for w in words):
        return None
    out = []
    for w in words:
        letters = w[1:].replace("F", "").replace("E", "").replace("G", "")
        if letters:
            out.append("-" + letters)
    return " ".join(["-E", *out])


def _tolerant(pat: str) -> str:
    key, _, val = pat.partition(":")
    return re.escape(key.strip()).replace('\\"', '"') + ": *" + re.escape(val.strip()).replace('\\"', '"')


def _tolerant_dq(pat: str) -> str:
    key, _, val = pat.partition(":")
    k, v = key.strip(), val.strip()
    esc = lambda s: re.sub(r"([.\[\]()*+?{}|^$])", r"\\\\\1", s)     # inside "…": a backslash must be doubled
    return esc(k) + ": *" + esc(v)


def tolerate_json_spacing(lines: list[str]) -> tuple[list[str], int]:
    out, n = [], 0
    for ln in lines or []:
        s = str(ln)
        for rx, conv in ((_SQ_RE, lambda p: "'" + _tolerant(p) + "'"), (_DQ_RE, lambda p: '"' + _tolerant_dq(p) + '"')):
            def rep(m):
                nonlocal n
                f = _flags(m.group("flags"))
                if f is None:
                    return m.group(0)
                n += 1
                return f"grep {f} " + conv(m.group("pat"))
            s = rx.sub(rep, s)
        out.append(s)
    return out, n


def repair(commands: list[str], verify: list[str], files: list[dict] | None) -> tuple[list[str], list[str], list[dict], list[dict]]:
    c, n1 = tolerate_json_spacing(commands)
    v, n2 = tolerate_json_spacing(verify)
    out_f, n3 = [], 0
    for f in files or []:
        body = str((f or {}).get("content") or "")
        new, k = tolerate_json_spacing(body.split("\n"))
        n3 += k
        out_f.append({**f, "content": "\n".join(new)} if k else f)
    notes = [{"why": ("a grep looked for a JSON key/value with one exact spacing; an API's formatting decides whether it "
                      "answers `\"k\": v` or `\"k\":v` (live, T5 counted 0 of 45 enabled indexers and T15 0 of 82 blocked "
                      "imports). The pattern now allows either (§17.1658).")}] if (n1 or n2 or n3) else []
    return c, v, out_f, notes
