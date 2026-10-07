"""§17.1409 — rehearse a settings-editing draft against copies of the real files.

Live, 2026-10-06/07. ADD122 ("implement the Palworld settings capability") went through
a dozen drafts. Each passed every shape gate the engine had, and each would have
broken the game server's settings or done nothing: a comma split that mangled
`CrossplayPlatforms=(Steam,Xbox,PS5,Mac)`, a save that dropped the
`[/Script/Pal.PalGameWorldSettings]` header, a PUT that ignored its body, a restart of
a unit that does not exist. Reading a draft cannot keep up with that; running it can.
The operator chose it: "coder model + round-trip test". (The coder model was measured
and is not better: 0/3 round trips either way. The rehearsal is the lever.)

The rehearsal service (`docker/rehearsal`, an internal network, chroot per job) runs
the draft's own commands and files against COPIES of the files they touch -- read off
the machines here, through the read-only channel -- with `ssh`/`sudo`/`systemctl`/
`pct`/`qm` standing in locally. Then it does what a user of the feature would do:
read the settings through the draft's GET, send them straight back through its PUT,
and compare the file with what it said before. A draft whose save does not reproduce
the file is refused with the evidence, and the redraft is told exactly what broke.

Fail-soft in the one direction that is safe: a rehearsal that cannot run (the
service is down, a file cannot be read) refuses nothing; it is logged as a gap.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("scaffold.rehearsal")

REHEARSAL_URL = os.environ.get("REHEARSAL_URL", "http://scaffold-rehearsal:8799")
#: the marker `_SHAPE_REFUSALS` keys on, so a failed rehearsal drives a redraft
MARK = "the rehearsal ran this block against copies of the real files"

#: a route the step's own text names: `http://127.0.0.1:3001/api/palworld/settings`
_ROUTE_RE = re.compile(r"https?://127\.0\.0\.1:(\d{2,5})(/[A-Za-z0-9_./-]+)")
#: files of a service's own directory worth copying: code and config, not dependencies or backups
_SKIP_RE = re.compile(r"/node_modules/|/\.git/|\.bak(?:\.|$)|\.log$|/package-lock\.json$")
MAX_SEED_FILES = 40
MAX_SEED_BYTES = 256_000


def roundtrip_target(node: Optional[dict], services: Optional[list]) -> Optional[dict]:
    """The round trip this step promises, or None: a route the step's text names, and a
    measured service config the step is about (its service named in the step)."""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description"))
    m = _ROUTE_RE.search(text)
    if not m:
        return None
    url = f"http://127.0.0.1:{m.group(1)}{m.group(2).rstrip('.')}"
    low = text.lower()
    for svc in services or []:
        name = str(getattr(svc, "name", "") or "")
        configs = tuple(getattr(svc, "configs", ()) or ())
        if not name or not configs or name.lower() not in low:
            continue
        live = configs[0]
        baseline = str(getattr(svc, "empty_beside", "") or "") or next(
            (c for c in configs[1:] if "default" in c.rsplit("/", 1)[-1].lower()), "")
        return {"get": url, "put": url, "config": live, "baseline": baseline,
                "config_guest": str(getattr(svc, "guest", "")), "config_vm": bool(getattr(svc, "vm", False)),
                "port": m.group(1)}
    return None


async def _cat(spec, gid: str, vm: bool, path: str) -> Optional[str]:
    from app.modules import service_truth as st
    ok, out = await st._probe(spec, f"{st.in_guest(gid, vm)} cat {path}")
    return out if ok else None


async def seeds_for(spec, target: dict, services: Optional[list]) -> list[dict]:
    """Copies of every file the rehearsal needs, read through the read-only channel:
    the config and its baseline, and the code + unit of the service that serves the route."""
    from app.modules import service_truth as st
    seeds: list[dict] = []
    for path in (target.get("config"), target.get("baseline")):
        if path:
            body = await _cat(spec, target["config_guest"], target["config_vm"], path)
            if body is not None:
                seeds.append({"path": path, "content": body})
    host = next((s for s in services or [] if target.get("port") in tuple(getattr(s, "ports", ()) or ())), None)
    if host is not None and getattr(host, "workdir", ""):
        gid, vm, wd = str(host.guest), bool(getattr(host, "vm", False)), str(host.workdir)
        ok, listing = await st._probe(spec, f"{st.in_guest(gid, vm)} find {wd} -maxdepth 3 -type f -size -256k")
        paths = [p.strip() for p in (listing or "").split("\n") if p.strip().startswith("/")] if ok else []
        for p in [p for p in paths if not _SKIP_RE.search(p)][:MAX_SEED_FILES]:
            body = await _cat(spec, gid, vm, p)
            if body is not None and len(body) <= MAX_SEED_BYTES:
                seeds.append({"path": p, "content": body})
        if getattr(host, "unit", ""):
            unit_path = f"/etc/systemd/system/{host.unit}"
            body = await _cat(spec, gid, vm, unit_path)
            if body is not None:
                seeds.append({"path": unit_path, "content": body})
    return seeds


async def rehearse(commands: list[str], files: list[dict], target: dict, seeds: list[dict],
                   url: Optional[str] = None) -> Optional[dict]:
    """POST the job to the rehearsal service; the report, or None when it cannot run."""
    job = {"seeds": seeds, "files": [{"path": f.get("path"), "content": f.get("content")} for f in files or []],
           "commands": list(commands or []),
           "roundtrip": {k: target[k] for k in ("get", "put", "config", "baseline")}}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(f"{url or REHEARSAL_URL}/rehearse", json=job)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.warning("rehearsal_unavailable err=%r", exc)
        return None


def refusal_from(report: Optional[dict]) -> list[dict]:
    """`[{command, why}]` when the rehearsal shows the draft does not do its job; else []."""
    if not report or report.get("error"):
        if report and report.get("error"):
            logger.warning("rehearsal_could_not_run err=%s", report.get("error"))
        return []
    rt = report.get("roundtrip") or {}
    if rt.get("ok"):
        return []
    bits: list[str] = []
    failed = [c for c in report.get("commands") or [] if c.get("exit")]
    if failed:
        c = failed[0]
        bits.append(f"`{c.get('command', '')[:90]}` exited {c.get('exit')}: "
                    f"{' '.join(str(c.get('out') or '').split())[-220:]}")
    if rt.get("get_status") != 200:
        bits.append(f"GET {rt.get('get_status')}: {' '.join(str(rt.get('get_body') or '').split())[:160]}")
    else:
        if rt.get("put_status") != 200:
            bits.append(f"PUT of the settings GET returned answered {rt.get('put_status')}: "
                        f"{' '.join(str(rt.get('put_body') or '').split())[:160]}")
        if rt.get("header_kept") is False:
            bits.append("the file lost its `[section]` header")
        if rt.get("expected_keys") is not None:
            bits.append(f"{rt.get('expected_keys')} settings before, {rt.get('got_keys')} after")
        if rt.get("missing"):
            bits.append(f"missing after the save: {', '.join(rt['missing'][:5])}")
        if rt.get("changed"):
            bits.append("changed by a save that should change nothing: "
                        + ", ".join(f"{k}={a!r}->{b!r}" for k, a, b in rt["changed"][:3]))
        if rt.get("extra"):
            bits.append(f"keys that were never there: {', '.join(map(str, rt['extra'][:4]))}")
        if rt.get("file_after") is not None:
            bits.append(f"the file after the save begins `{' '.join(str(rt['file_after']).split())[:160]}`")
    return [{"command": "(rehearsal)", "why": (
        f"{MARK} (read off the machines just now) and did what a user of the feature would: read the settings "
        f"through its GET and sent them straight back through its PUT -- a save that changes nothing. "
        f"The file must still say the same thing. It did not: " + "; ".join(bits or ["the round trip failed"])
        + ". Parse the settings with their nesting in mind (a value can itself be `(A,B,C)`), keep the "
          "section header, write what the PUT body asks for, and restart the unit the facts name.")}]
