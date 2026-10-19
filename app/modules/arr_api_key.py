r"""§17.1657 — a call to an *arr app's own API, inside its own guest, presents the app's key.

Live (job d89078ed T3/T14, 2026-10-19): "Verify Radarr API version" and "Verify Radarr health clean" were drafted as
`curl -s http://localhost:7878/api/v3/system/status | …` -- no key. Radarr answers 401 to every unauthenticated API
call, so the check could only fail, and each step was settled by hand from a read with the key. The engine already
knows where each *arr keeps its key (`machine_values`: config.xml in its guest) and reads it in place of the
`<RADARR_API_KEY>` placeholder (§17.1332); the draft just never asked for it.

So: a `curl` to an *arr's loopback API, on a line that runs INSIDE that app's guest and carries no key
(`X-Api-Key` / `apikey=`), gets one --
  * in a command or check: the `<APP_API_KEY>` placeholder, which §17.1332 turns into the in-guest read;
  * in a script file the runner writes (the run_in_container template's in-guest body, which §17.1332 leaves alone):
    the in-guest read itself.
"""
from __future__ import annotations

import re
from typing import Optional

#: app -> its loopback port
ARR_PORTS = {"radarr": "7878", "sonarr": "8989", "prowlarr": "9696", "lidarr": "8686", "readarr": "8787"}
_ARR_URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(?P<port>\d{2,5})/api/")
_HAS_KEY_RE = re.compile(r"x-api-key|[?&]apikey=", re.I)
_PCT_RE = re.compile(r"(?<![\w-])pct\s+exec\s+(?P<gid>\d{3,5})\s+--")


def _read(app: str) -> str:
    return (f'$(cat /var/lib/{app}/config.xml /config/config.xml 2>/dev/null | '
            f'sed -n "s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p" | head -n 1)')


def _apps_by_port(services: Optional[list]) -> dict:
    out = {}
    for s in services or []:
        name = str(getattr(s, "name", "") or "").lower()
        if name in ARR_PORTS and getattr(s, "guest", ""):
            out[ARR_PORTS[name]] = (name, str(s.guest))
    return out


def _in_double_quotes(line: str, at: int) -> bool:
    """Is position `at` inside a double-quoted word (so an inserted `"` must be written `\\"`)?"""
    q = ""
    k = 0
    while k < at:
        c = line[k]
        if c == "\\" and q != "'":
            k += 2
            continue
        if q:
            if c == q:
                q = ""
        elif c in "\"'":
            q = c
        k += 1
    return q == '"'


def _present(line: str, apps: dict, guest: str, literal: bool) -> str:
    if "curl" not in line or _HAS_KEY_RE.search(line):
        return line
    m = _ARR_URL_RE.search(line)
    if not m or m.group("port") not in apps:
        return line
    app, gid = apps[m.group("port")]
    if gid != guest:
        return line                                   # another guest's app: the key is not here
    c = line.rfind("curl", 0, m.start())
    if c < 0:
        return line
    value = _read(app) if literal else f"<{app.upper()}_API_KEY>"
    q = '\\"' if _in_double_quotes(line, c) else '"'
    return line[:c + 4] + f" -H {q}X-Api-Key: {value}{q}" + line[c + 4:]


def present_the_arr_key(commands: list[str], verify: list[str], files: Optional[list[dict]],
                        services: Optional[list]) -> tuple[list[str], list[str], list[dict], list[dict]]:
    apps = _apps_by_port(services)
    if not apps:
        return list(commands or []), list(verify or []), list(files or []), []
    from app.modules.service_truth import where_each_line_runs

    def cmd(c: str) -> str:
        m = _PCT_RE.search(str(c))
        return _present(str(c), apps, m.group("gid"), literal=False) if m else str(c)

    out_c, out_v = [cmd(c) for c in commands or []], [cmd(v) for v in verify or []]
    out_f = []
    for f in files or []:
        body = str((f or {}).get("content") or "")
        where = where_each_line_runs(body)
        lines = body.split("\n")
        new = [_present(ln, apps, str(where.get(i) or ""), literal=True) if where.get(i) else ln
               for i, ln in enumerate(lines, 1)]
        out_f.append({**f, "content": "\n".join(new)} if new != lines else f)
    changed = (out_c != [str(c) for c in commands or []] or out_v != [str(v) for v in verify or []]
               or out_f != list(files or []))
    notes = [{"why": ("a call to an *arr app's own API carried no key, and the app answers 401 to every unauthenticated "
                      "call (live, T3/T14 could only fail); presented the app's key, read inside its own guest from its "
                      "config.xml (§17.1657).")}] if changed else []
    return out_c, out_v, out_f, notes
