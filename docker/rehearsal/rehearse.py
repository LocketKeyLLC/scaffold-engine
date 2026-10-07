"""§17.1409 — rehearse a draft against copies of the real files, before anyone approves it.

Reads one JSON job on stdin, prints one JSON report on stdout:

    {"seeds":    [{"path": "/opt/palworld/…/PalWorldSettings.ini", "content": "…"}],
     "files":    [{"path": "/tmp/x.py", "content": "…"}],          # the draft's own files
     "commands": ["bash /tmp/install.sh", …],                        # the draft's commands
     "probes":   [{"name": "get", "method": "GET", "url": "http://127.0.0.1:3001/…"}],
     "roundtrip": {"get": "http://…/api/palworld/settings", "put": "http://…/api/palworld/settings",
                   "config": "/opt/palworld/…/PalWorldSettings.ini",
                   "baseline": "/opt/palworld/DefaultPalWorldSettings.ini"}}

The sandbox stands in for every machine at once: `ssh`, `sudo`, `systemctl`, `pct` and
`qm` are shims (see shims/) that run the command HERE, where the seeded files sit at
their own paths. Nothing here reaches a real machine; the engine runs this container with
no network.

The round trip is the property a settings editor must have: read the settings through
the draft's own GET, send them straight back through its own PUT (a save that changes
nothing), and the file must still say the same thing — same section header, same keys,
same values, nested values like `CrossplayPlatforms=(Steam,Xbox,PS5,Mac)` intact.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request


def ue_settings(text: str) -> tuple[str, dict]:
    """(section header, {key: value}) of an Unreal `OptionSettings=(…)` file, paren-aware."""
    header = ""
    for line in (text or "").splitlines():
        if line.strip().startswith("[") and line.strip().endswith("]"):
            header = line.strip()
            break
    m = re.search(r"OptionSettings=\(", text or "")
    if not m:
        return header, {}
    i, depth, start = m.end(), 1, m.end()
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    body = text[start:i - 1]
    out, buf, depth, quoted = {}, "", 0, False
    for ch in body + ",":
        if ch == '"':
            quoted = not quoted
        if not quoted:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                if "=" in buf:
                    k, v = buf.split("=", 1)
                    out[k.strip()] = v.strip()
                buf = ""
                continue
        buf += ch
    return header, out


def http(method: str, url: str, body: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                          # connection refused, timeout
        return 0, repr(e)


def main() -> None:
    job = json.load(sys.stdin)
    os.makedirs("/rehearsal", exist_ok=True)
    open("/rehearsal/log", "w").close()
    report: dict = {"commands": [], "probes": [], "roundtrip": None}
    report["skipped"] = []
    for f in job.get("seeds", []) + job.get("files", []):
        path = str(f.get("path") or "")
        # one unusable seed is recorded, never fatal: a directory, a relative path, nothing
        if not path.startswith("/") or path.endswith("/") or os.path.isdir(path):
            report["skipped"].append(path)
            continue
        os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(f.get("content") or "")
    _seed_before = {f["path"]: f.get("content") or "" for f in job.get("seeds", []) if str(f.get("path") or "").startswith("/")}
    for cmd in job.get("commands", []):
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=120)
        report["commands"].append({"command": cmd[:200], "exit": p.returncode,
                                   "out": (p.stdout + p.stderr)[-600:]})
    # the backend is up if a restart started it; otherwise start what is there
    if os.path.exists("/opt/control-panel-backend/server.js") and not os.path.exists("/rehearsal/server.pid"):
        p = subprocess.Popen("cd /opt/control-panel-backend && exec node server.js >/rehearsal/server.log 2>&1",
                             shell=True, start_new_session=True)
        open("/rehearsal/server.pid", "w").write(str(p.pid))
        time.sleep(2)
    for pr in job.get("probes", []):
        st, body = http(pr.get("method", "GET"), pr["url"])
        report["probes"].append({"name": pr.get("name"), "status": st, "body": body[:800]})
    rt = job.get("roundtrip")
    if rt:
        base_text = open(rt["baseline"]).read() if rt.get("baseline") and os.path.exists(rt["baseline"]) else ""
        cfg_before = open(rt["config"]).read() if os.path.exists(rt["config"]) else ""
        expect_header, expect = ue_settings(cfg_before if "OptionSettings=" in cfg_before else base_text)
        st, body = http("GET", rt["get"])
        verdict: dict = {"get_status": st, "get_body": body[:400]}
        if st == 200:
            pst, pbody = http("PUT", rt["put"], body.encode())
            verdict.update({"put_status": pst, "put_body": pbody[:400]})
            after = open(rt["config"]).read() if os.path.exists(rt["config"]) else ""
            got_header, got = ue_settings(after)
            missing = sorted(set(expect) - set(got))
            changed = sorted(k for k in expect if k in got and got[k] != expect[k])
            extra = sorted(set(got) - set(expect))
            verdict.update({
                "header_kept": got_header == expect_header and bool(expect_header),
                "expected_keys": len(expect), "got_keys": len(got),
                "missing": missing[:12], "changed": [(k, expect[k], got[k]) for k in changed[:8]],
                "extra": extra[:12], "file_after": after[:300],
            })
            verdict["ok"] = (pst == 200 and verdict["header_kept"] and not missing and not changed and not extra)
        else:
            verdict["ok"] = False
        report["roundtrip"] = verdict
    # §17.1411b — what the block DID to the copied files: a 404 with an untouched server.js is
    # "nothing registered the route", which "GET 404" alone never said (five repairs kept the
    # same no-op `sed` because nobody told them it inserted nothing).
    import difflib
    rt_paths = {(job.get("roundtrip") or {}).get(k) for k in ("config", "baseline")}
    report["changed_files"], report["unchanged_files"] = [], []
    for path, before in _seed_before.items():
        if path in rt_paths:
            continue
        after = open(path).read() if os.path.isfile(path) else None
        if after == before:
            report["unchanged_files"].append(path)
        else:
            diff = "".join(list(difflib.unified_diff(before.splitlines(True), (after or "").splitlines(True),
                                                     "before", "after", n=1))[2:40])
            report["changed_files"].append({"path": path, "diff": diff[:1200] if after is not None else "(deleted)"})
    # §17.1411c — does the backend parse a JSON body at all? Live: CT 111's server.js never
    # calls express.json(), so every PUT reached the handler with req.body undefined.
    js_after = [p for p in _seed_before if p.endswith(".js") and os.path.isfile(p)]
    js_after += [f["path"] for f in job.get("files", []) if str(f.get("path", "")).endswith(".js")
                 and os.path.isfile(str(f.get("path")))]
    report["parses_json_body"] = any(
        re.search(r"express\.json\(|bodyParser\.json\(|express\.urlencoded\(", open(p).read())
        for p in dict.fromkeys(js_after)) if js_after else None
    report["log"] = open("/rehearsal/log").read()[-1500:]
    report["server_log"] = open("/rehearsal/server.log").read()[-800:] if os.path.exists("/rehearsal/server.log") else ""
    print(json.dumps(report))


if __name__ == "__main__":
    main()
