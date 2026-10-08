"""§17.1412 — a step that writes SOFTWARE is developed against the real files, tested, then
delivered by an engine-owned template. It is not drafted as one blind shell block.

Why, measured. ADD122 ("implement the Palworld settings capability", 2026-10-06/07) went
through some twenty drafts and twenty engine fixes (PRs #768–#787), each draft failing on a
new defect: a comma split that mangled nested settings, a dropped `[section]` header, a
`sudo x > file` redirect, JSON used as shell quoting, a guessed unit name, a `sed` that
matched no line of server.js, a backend that never parsed a JSON body. The operator: "you are
fixing a large number of small issues instead of addressing all of the issues as a whole."
They share three causes:

1. The drafter never SAW the files it changed. It edited server.js blind -- hence the no-op
   `sed`, the missing `express.json()`, the invented paths.
2. It wrote the feature AND its delivery in one shell block -- hence every quoting,
   host-vs-guest, sudo, push and ownership defect.
3. Each attempt was a fresh guess, judged afterwards.

So, for a step that builds software in a measured service's own directory:

* the model is handed the CURRENT files of that directory, read off the machine, and returns
  the complete new content of every file it changes or adds -- code, not commands;
* each version is turned into a delivery runbook by `render_delivery` (back up, push the
  files, restart the measured unit, run the step's own checks) -- a shape the engine owns, so
  delivery mistakes cannot be drafted;
* that runbook is judged by the SAME machinery as any draft -- every gate in `frame_run`, the
  machine preconditions, and the rehearsal sandbox's round trip -- and the refusals come
  back to the model as evidence, with its own previous version, until a version passes or
  the budget is spent. The best version is what the operator is shown.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("scaffold.develop")

#: versions the model may write before the best one is shown
ROUNDS = int(os.environ.get("DEVELOP_ROUNDS", "8"))
#: where the delivery stages files on the host before pushing them into the guest
STAGE_ROOT = "/tmp/scaffold-dev"

_SKIP_RE = re.compile(r"/node_modules/|/\.git/|\.bak(?:\.|$)|\.log$|/package-lock\.json$|\.tgz$")
_PATH_IN_TEXT_RE = re.compile(r"(?<![\w.-])(/(?:opt|srv|var/www|etc/systemd/system)/[A-Za-z0-9_./-]+)")
#: a check the step itself names (`pct exec 111 -- curl -s http://…`) -- read-only by construction
_CHECK_RE = re.compile(r"`((?:pct exec \d+ --|qm guest exec \d+ --)\s*[^`]+)`")
#: markers that would end a fenced file or a section of the runbook
_UNSAFE_RE = re.compile(r"```|^\s{0,3}#{2,3}\s", re.M)


@dataclass
class Host:
    """The measured service whose directory the step's software lives in."""
    guest: str
    vm: bool
    workdir: str
    unit: str
    user: str = ""
    group: str = ""
    name: str = ""


def _text(node: Optional[dict]) -> str:
    return " ".join(str((node or {}).get(k) or "") for k in ("title", "description"))


def host_for(node: Optional[dict], services: Optional[list]) -> Optional[Host]:
    """The step builds software, and a measured service with a directory hosts it -- or None.

    The service is the one on the step's own guest (`LXC 111: …`), else one the step names."""
    from app.modules.step_decomposition import builds_a_capability
    text = _text(node)
    if not builds_a_capability(text):
        return None
    m = re.search(r"\b(?:LXC|CT|VM|container|guest)\s*(\d{3,5})\b", str((node or {}).get("title") or ""), re.I)
    cands = [s for s in services or [] if getattr(s, "workdir", "") and getattr(s, "unit", "")]
    pick = None
    if m:
        pick = next((s for s in cands if str(s.guest) == m.group(1)), None)
    if pick is None:
        low = text.lower()
        pick = next((s for s in cands if str(getattr(s, "name", "")).lower() in low
                     or str(s.workdir) in text), None)
    if pick is None:
        return None
    return Host(guest=str(pick.guest), vm=bool(getattr(pick, "vm", False)), workdir=str(pick.workdir).rstrip("/"),
                unit=str(pick.unit), user=str(getattr(pick, "user", "") or ""),
                group=str(getattr(pick, "group", "") or ""), name=str(getattr(pick, "name", "") or ""))


def allowed_path(path: str, host: Host, node: Optional[dict]) -> bool:
    """A file the model may write: under the service's directory, under a directory the step
    itself names, or the service's own unit file. Absolute, no `..`."""
    p = str(path or "")
    if not p.startswith("/") or "/../" in p or p.endswith("/") or "\x00" in p:
        return False
    if p.startswith(host.workdir + "/") or p == f"/etc/systemd/system/{host.unit}":
        return True
    for named in _PATH_IN_TEXT_RE.findall(_text(node)):
        base = named.rstrip("/.")
        base = base if "." not in base.rsplit("/", 1)[-1] else base.rsplit("/", 1)[0]
        if base.count("/") >= 2 and (p == named or p.startswith(base + "/")):
            return True
    return False


async def read_workspace(spec, host: Host) -> dict[str, str]:
    """The service directory's code and config as it is NOW, read through the read-only channel."""
    from app.modules import rehearsal
    from app.modules import service_truth as st
    ok, listing = await st._probe(spec, rehearsal.listing_command(st.in_guest(host.guest, host.vm), host.workdir))
    paths = [p.strip() for p in (listing or "").split("\n")
             if p.strip().startswith(host.workdir + "/") and not p.strip().endswith("/")] if ok else []
    out: dict[str, str] = {}
    for p in [p for p in paths if not _SKIP_RE.search(p)][:rehearsal.MAX_SEED_FILES]:
        body = await rehearsal._cat(spec, host.guest, host.vm, p)
        if body is not None and len(body) <= rehearsal.MAX_SEED_BYTES:
            out[p] = body
    unit = await rehearsal._cat(spec, host.guest, host.vm, f"/etc/systemd/system/{host.unit}")
    if unit is not None:
        out[f"/etc/systemd/system/{host.unit}"] = unit
    return out


def _is_read(cmd: str) -> bool:
    from app.modules.assist_state_check import read_only_command
    try:
        return bool(read_only_command(cmd))
    except Exception:
        return False


#: §17.1426 — a check that names a runner secret (`$PANEL_PASSWORD`). The runner injects secrets only into
#: APPROVED commands whose text references them; the read-only check channel never gets one.
_SECRET_REF_RE = re.compile(r"\$\{?([A-Z][A-Z0-9_]{2,60})\}?")


def _needs_a_secret(cmd: str) -> bool:
    return bool(_SECRET_REF_RE.search(cmd))


def done_checks(node: Optional[dict], host: Host) -> list[str]:
    """The step's own READ checks (backticked `pct exec N -- …` in its text), then the unit is active.

    §17.1414 — only reads: the verify channel carries nothing else, and silently dropped ADD123's
    POST. A check that WRITES is the run's own acceptance step (`acceptance_checks`)."""
    checks = [c.strip() for c in _CHECK_RE.findall(str((node or {}).get("description") or ""))
              if _is_read(c.strip()) and not _needs_a_secret(c.strip())]
    tool = f"qm guest exec {host.guest} --" if host.vm else f"pct exec {host.guest} --"
    checks.append(f"{tool} systemctl is-active {host.unit}")
    return list(dict.fromkeys(checks))


NO_OWN_CHECK = "names no command the engine can run to see the feature work"


def own_check_refusal(node: Optional[dict], host: Host) -> list[dict]:
    """§17.1418 — a developed step needs a check of ITS feature. `done_checks` always adds the unit's
    `is-active`, which the delivery itself makes true; a step whose text names nothing else would be
    recorded done while its route answers a connection error. Live: ADD124 ("returns the engine's
    /health with status healthy", in prose) was offered with `is-active` as its only check."""
    if acceptance_checks(node) or len(done_checks(node, host)) > 1:
        return []
    return [{"command": "(the step's done-condition)", "why": (
        f"this step {NO_OWN_CHECK}: its only check would be `systemctl is-active {host.unit}`, which the "
        f"delivery makes true by restarting the unit, so the step would be recorded done whether the feature "
        f"works or not. Its text must name the check in backticks, as a command run on the machine -- e.g. "
        f"`{'qm guest exec' if host.vm else 'pct exec'} {host.guest} -- curl -s http://127.0.0.1:<port>/api/<route>`.")}]


def engine_doc(node: Optional[dict]) -> str:
    """§17.1418 — what a machine-side developer must know about THIS engine, when the step is about it.
    Live: ADD124's route was pointed at `127.0.0.1:3000` -- the container itself, on an invented port."""
    # the engine as a SERVICE the step reaches -- not any mention ("proven by the engine's rehearsal")
    if not re.search(r"scaffold-engine|engine's own (?:ai )?(?:surface|api|health|address)", _text(node), re.I):
        return ""
    url, allow = os.environ.get("SCAFFOLD_LAN_URL", ""), os.environ.get("SCAFFOLD_LAN_ALLOW", "")
    if not url:
        return ("THE ENGINE ITSELF (scaffold-engine) is NOT reachable from the operator's machines: it listens on "
                "its own host's loopback only. Do not invent an address for it.")
    return (f"THE ENGINE ITSELF (scaffold-engine): {url} -- reachable from {allow or 'one allowed machine'} only. "
            f"`GET {url}/health` needs no key and answers JSON with \"status\": \"healthy\"; every other route "
            f"needs the engine's API key. Never 127.0.0.1 or localhost for it: from a guest, that is the guest.")


def own_checks(node: Optional[dict]) -> list[str]:
    """§17.1419 — every check the step's text names (backticked `pct exec N -- …`), reads and writes."""
    return list(dict.fromkeys(c.strip() for c in _CHECK_RE.findall(str((node or {}).get("description") or ""))))


def acceptance_checks(node: Optional[dict]) -> list[str]:
    """§17.1414 — the step's own checks that WRITE (ADD123: a POST that asks Radarr for a film). They
    cannot be verified read-only, and the sandbox cannot reach another machine's API, so they run as
    the approved block's LAST step -- visible to the operator before approval -- with `curl -f`, so
    an HTTP error fails the run instead of printing an error page and exiting 0."""
    out: list[str] = []
    for c in _CHECK_RE.findall(str((node or {}).get("description") or "")):
        c = c.strip()
        if _is_read(c) and not _needs_a_secret(c):      # §17.1426 — a read that needs a secret runs approved
            continue
        if re.search(r"\bcurl\b", c) and not re.search(r"\bcurl\b[^|;&]*\s-(?:[a-zA-Z]*f[a-zA-Z]*)\b|--fail\b", c):
            c = re.sub(r"\bcurl\b", "curl -f", c, count=1)
        out.append(c)
    return list(dict.fromkeys(out))


def render_delivery(node: Optional[dict], host: Host, files: dict[str, str], checks: list[str],
                    acceptance: Optional[list[str]] = None, credentials: Optional[dict[str, str]] = None) -> str:
    """The delivery runbook -- a shape the engine owns: stage, back up, push, restart, check."""
    nk = re.sub(r"[^A-Za-z0-9_-]", "", str((node or {}).get("node_key") or "step")) or "step"
    # §17.1413c — staged FLAT, directly in /tmp: the runner's write_file creates no directory
    # (live: "'/tmp/scaffold-dev/ADD122/opt/…' is not a directory on this machine"), so a nested
    # stage path is refused before anything runs. The target path is encoded in the name.
    stage = f"{STAGE_ROOT}-{nk}"

    def staged(path: str) -> str:
        return stage + "--" + path.strip("/").replace("/", "--")
    wd_rel = host.workdir.lstrip("/")
    visible: dict[str, str] = {}
    carried: dict[str, str] = {}
    for path, content in files.items():
        (carried if _UNSAFE_RE.search(content or "") else visible)[path] = content or ""
    sh: list[str] = ["#!/usr/bin/env bash",
                     "# §17.1412 — delivered by the engine's own template: back up, push, restart, check.",
                     "set -euo pipefail"]
    for path, content in carried.items():
        b64 = base64.b64encode(content.encode()).decode()
        sh.append(f"echo {b64} | base64 -d > {shlex.quote(staged(path))}")
    # §17.1415 — a file carrying a credential MARKER gets the real key on the host, read where the service
    # keeps it; the staged copy (now holding the key) is removed on exit whatever happens.
    secret_files = [p for p, c in files.items() if _MARK_RE.search(c or "")]
    if secret_files:
        sh.append("trap " + shlex.quote("rm -f " + " ".join(shlex.quote(staged(p)) for p in secret_files)) + " EXIT")
        for name in sorted({n for p in secret_files for n in _MARK_RE.findall(files[p])}):
            read = (credentials or {}).get(name)
            if not read:
                sh.append(f"echo 'the engine cannot read {name} on any machine' >&2; exit 1")
                continue
            # §17.1417 — `|| true`: the read cats BOTH places an app may keep its config, one never exists,
            # `cat` exits 1, and under `set -euo pipefail` the assignment killed the script silently before
            # anything was backed up or pushed. Caught by the rehearsal; the empty-read check below decides.
            if read == RUNNER_HELD:                                  # §17.1426 — handed in by the run line
                sh.append(f'V_{name}="${{{name}:-}}"')
                sh.append(f'[ -n "$V_{name}" ] || {{ echo "the runner holds no {name} -- store it in Settings -> Machines" >&2; exit 1; }}')
            else:
                sh.append(f"V_{name}=$({read} || true)")
                sh.append(f'[ -n "$V_{name}" ] || {{ echo "could not read {name} on the machine" >&2; exit 1; }}')
            for p in secret_files:
                if SECRET_MARK.format(name=name) in (files[p] or ""):
                    # §17.1426 — perl reads the value from the environment: `sed s|…|$V|` corrupts a password
                    # holding `|`, `&` or `\`, and nothing here re-parses what perl substitutes
                    # and inside a .json file the value is a JSON string (json.dumps), or a password holding `"`
                    # or `\` leaves the config unparseable and the service down (caught at bash level).
                    # §17.1428 — python, not perl: a perl program's `$v` read as an unset SHELL variable to
                    # §17.1348 and refused ADD127 eight rounds; this program carries no `$` at all.
                    sh.append(f"SCAFFOLD_V=\"$V_{name}\" python3 -c {shlex.quote(_FILL_PROG)} "
                              f"{shlex.quote(staged(p))} {shlex.quote(SECRET_MARK.format(name=name))}")
    # §17.1424 — back up EVERY path the delivery writes, not only the service directory. Live, ADD126 would
    # have overwritten /opt/control-panel-ui/index.html (the Vite app's entry from T34) with a backup of
    # /opt/control-panel-backend alone. `--ignore-failed-read`: a file the delivery creates is not there yet.
    wd_root = host.workdir.rstrip("/") + "/"
    outside = sorted(p.lstrip("/") for p in files if not p.startswith(wd_root))
    keep = " ".join(shlex.quote(r) for r in [wd_rel, *outside])
    tar = f"tar czf \"$BACKUP\" --ignore-failed-read --exclude=node_modules -C / {keep}"
    if host.vm:
        g = f"qm guest exec {host.guest}"
        sh += [f"qm status {host.guest} | grep -q running || qm start {host.guest}",
               f"BACKUP=/root/scaffold-backup-{nk}-$(date +%Y%m%d%H%M%S).tgz",
               f"{g} -- {tar} >/dev/null"]
        for path in files:
            sh.append(f"{g} -- mkdir -p {shlex.quote(os.path.dirname(path))} >/dev/null")
            sh.append(f"{g} --pass-stdin 1 -- sh -c {shlex.quote('cat > ' + shlex.quote(path))} "
                      f"< {shlex.quote(staged(path))} >/dev/null")
        own = f"{g} --"
    else:
        g = f"pct exec {host.guest} --"
        sh += [f"pct status {host.guest} | grep -q running || pct start {host.guest}",
               f"BACKUP=/root/scaffold-backup-{nk}-$(date +%Y%m%d%H%M%S).tgz",
               f"{g} {tar}"]
        for path in files:
            sh.append(f"{g} mkdir -p {shlex.quote(os.path.dirname(path))}")
            sh.append(f"pct push {host.guest} {shlex.quote(staged(path))} {shlex.quote(path)}")
        own = g
    for path in secret_files:                     # §17.1426 — a file holding a credential is its owner's alone
        sh.append(f"{own} chmod 600 {shlex.quote(path)}" + (" >/dev/null" if host.vm else ""))
    if host.user and host.user != "root":
        owner = host.user + (f":{host.group}" if host.group else "")
        for path in files:
            sh.append(f"{own} chown {shlex.quote(owner)} {shlex.quote(path)}")
    if any(p.startswith("/etc/systemd/system/") for p in files):
        sh.append(f"{own} systemctl daemon-reload")
    sh += [f"{own} systemctl restart {shlex.quote(host.unit)}",
           f"for i in 1 2 3 4 5 6 7 8 9 10; do {own} systemctl is-active --quiet {shlex.quote(host.unit)} && break; sleep 1; done",
           f"{own} systemctl is-active {shlex.quote(host.unit)}",
           'echo "backup: $BACKUP"']
    out = ["## Write these files", ""]
    for path, content in visible.items():
        out += [f"### {staged(path)}", "```", content.rstrip("\n"), "```", ""]
    out += [f"### {stage}--deliver.sh", "```bash", "\n".join(sh), "```", ""]
    if carried:
        out += [f"({len(carried)} file(s) carried inside deliver.sh base64-encoded, because their text holds "
                f"a fence or a heading line: {', '.join(carried)})", ""]
    # §17.1426 — the runner injects a secret only into a command whose TEXT names it: the run line names
    # each runner-held one the delivery uses, so deliver.sh gets the value and the engine never does
    held = sorted(n for n, r in (credentials or {}).items() if r == RUNNER_HELD
                  and any(SECRET_MARK.format(name=n) in (c or "") for c in files.values()))
    prefix = "".join(f'{n}="${n}" ' for n in held)
    run = [f"{prefix}bash {stage}--deliver.sh", *(acceptance or [])]      # §17.1414 — the step's own write check, last
    out += ["## Run this", "", "```bash", "\n".join(run), "```", "",
            "## Verify", "", "```bash", "\n".join(checks), "```", ""]
    return "\n".join(out)


# ── §17.1413: engine-owned building blocks, delivered with every developed version ──

_KIT_DIR = os.path.join(os.path.dirname(__file__), "develop_kit")
KIT_SUBDIR = "scaffold-kit"
KIT_DOC = """THE ENGINE'S KIT (installed for you at {kit}/ on every delivery -- use it, do not write your own):
- `const remote = require('{kit}/remote');` -- for ANYTHING on another machine. `host = {{address, user}}`.
  `remote.readFile(host, path)` -> text (sudo cat). `remote.writeFile(host, path, text, {{owner: 'user:group'}})`
  (sudo tee, the text travels on stdin). `remote.exists(host, path)`. `remote.unit(host, 'stop'|'start'|'restart'|'is-active', unit)`.
  Never build an ssh command line yourself; never quote content into one.
- `const ue = require('{kit}/ue_settings');` -- Unreal-engine settings files (one `[/Script/...]` header and one
  `OptionSettings=(k=v,...)` line; a value can itself be `(A,B,C)`). `ue.parse(text)` -> `{{header, settings}}`
  or null when the file has no OptionSettings line; `ue.serialize(doc)` -> the file text. parse(serialize(doc)) equals doc.
"""


def kit_for(host: Host, workspace: dict[str, str]) -> dict[str, str]:
    """The kit files for this service's runtime, at their delivered paths (Node services today)."""
    unit = workspace.get(f"/etc/systemd/system/{host.unit}", "")
    if "node" not in unit.lower():
        return {}
    base = f"{host.workdir}/{KIT_SUBDIR}"
    out: dict[str, str] = {}
    for name in ("remote.js", "ue_settings.js"):
        with open(os.path.join(_KIT_DIR, "node", name), encoding="utf-8") as fh:
            out[f"{base}/{name}"] = fh.read()
    return out


def kit_doc(host: Host, kit: dict[str, str]) -> str:
    return KIT_DOC.format(kit=f"{host.workdir}/{KIT_SUBDIR}") if kit else ""


# ── §17.1415: credentials the engine reads on the machine, filled in at delivery ──

SECRET_MARK = "@@SCAFFOLD:{name}@@"
_MARK_RE = re.compile(r"@@SCAFFOLD:([A-Z][A-Z0-9_]{2,60})@@")


#: §17.1428 — the marker filler: value from the environment (never argv), plain string replace, JSON-escaped
#: inside a .json file. No `$` anywhere, so no shell-level gate can read it as a variable.
#: §17.1429 — opened `r+` (no O_CREAT): the staged file belongs to the runner's user in sticky /tmp, and
#: `fs.protected_regular` refuses even root an O_CREAT open of it (live: PermissionError on ADD127).
_FILL_PROG = ("import json, os, sys\n"
              "path, mark = sys.argv[1], sys.argv[2]\n"
              "value = os.environ['SCAFFOLD_V']\n"
              "if path.endswith('.json'):\n"
              "    value = json.dumps(value, ensure_ascii=False)[1:-1]\n"
              "f = open(path, 'r+', encoding='utf-8')\n"
              "text = f.read()\n"
              "f.seek(0)\n"
              "f.write(text.replace(mark, value))\n"
              "f.truncate()\n"
              "f.close()\n")
RUNNER_HELD = "@runner"          # §17.1426 — the value lives in the runner's secret store, injected at run time


def credentials_for(services: Optional[list], runner_secrets: Optional[list] = None,
                    node: Optional[dict] = None) -> dict[str, str]:
    """`{NAME: read command}` for the API keys the engine can read ON THE MACHINE for the measured
    services (Radarr's key from its own config.xml on its guest, …) -- the machine-values registry
    (§17.1332), never a value the engine or the model holds."""
    from app.modules import machine_values as mv
    out: dict[str, str] = {}
    for svc in services or []:
        name, gid = str(getattr(svc, "name", "") or ""), str(getattr(svc, "guest", "") or "")
        if not name or not gid:
            continue
        key = re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_API_KEY"
        r = mv.readable_for(key)
        if r is not None and r.app == name.lower():
            out[key] = r.read(gid)
    # §17.1426 — and a secret the RUNNER holds that the step names (ADD127: the panel's own password,
    # operator decision 2026-10-08). Its value never reaches the engine: the run line hands it to deliver.sh.
    text = _text(node)
    for n in runner_secrets or []:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,60}", str(n)) and re.search(rf"\b{re.escape(str(n))}\b", text):
            out[str(n)] = RUNNER_HELD
    return out


def credentials_doc(creds: dict[str, str], services: Optional[list]) -> str:
    if not creds:
        return ""
    where = {re.sub(r"[^A-Z0-9]", "_", str(getattr(s, "name", "")).upper()) + "_API_KEY": getattr(s, "guest", "?")
             for s in services or []}
    lines = [(f"- `{SECRET_MARK.format(name=n)}` -- the runner's secret {n} (held by the runner, put in at delivery)"
              if creds[n] == RUNNER_HELD else
              f"- `{SECRET_MARK.format(name=n)}` -- {n.replace('_API_KEY', '').title()}'s API key (read on guest "
              f"{where.get(n, '?')} at delivery)") for n in sorted(creds)]
    return ("CREDENTIALS: where your code needs one of these, put the MARKER literally in a config file you deliver "
            "(never in code, never a value you invent, never an empty string). The delivery replaces it on the machine "
            "with the real key; the value never passes through you:\n" + "\n".join(lines))


# ── §17.1416: the APIs the code calls, as they are now ──────────────────────

#: the *arr endpoints whose answers a client must use rather than invent (live, ADD123: a lookup result
#: carries no root folder, and the model filled `/movies` where Radarr's only root folder is
#: `/media/movies` -- every new film would have been refused).
_ARR_API = {"radarr": ("v3", ("rootfolder", "qualityprofile")),
            "sonarr": ("v3", ("rootfolder", "qualityprofile")),
            "lidarr": ("v1", ("rootfolder", "qualityprofile")),
            "readarr": ("v1", ("rootfolder", "qualityprofile")),
            "whisparr": ("v3", ("rootfolder", "qualityprofile"))}


#: §17.1421 — services whose API answers WITHOUT a key on this home lab, probed by status: which path the
#: installed version serves (live: Pi-hole v6.4.3 answers `/api/stats/summary` 200 and the v5
#: `/admin/api.php?summary` 400; a round of ADD125 wrote the v5 path).
_STATUS_PROBES = {"pihole": (80, ("/api/stats/summary", "/admin/api.php?summary"))}


def status_reads(services: Optional[list]) -> list[tuple[str, str, str]]:
    """`(service, path, command)` -- `curl -o /dev/null -w %{http_code}` inside the service's own guest."""
    out: list[tuple[str, str, str]] = []
    for svc in services or []:
        name, gid = str(getattr(svc, "name", "") or "").lower(), str(getattr(svc, "guest", "") or "")
        key = next((k for k in _STATUS_PROBES if name.startswith(k)), None)
        if key is None or not gid or getattr(svc, "vm", False):
            continue
        port, paths = _STATUS_PROBES[key]
        for path in paths:
            out.append((key, path, f'pct exec {gid} -- curl -s -m 10 -o /dev/null -w %{{http_code}} "http://127.0.0.1:{port}{path}"'))
    return out


def api_reads(services: Optional[list]) -> list[tuple[str, str, str]]:
    """`(service, endpoint, command)` -- each a read run INSIDE the service's guest (§17.1342): the key
    and the port come out of the app's own config.xml there, so neither leaves the guest."""
    from app.modules import machine_values as mv
    out: list[tuple[str, str, str]] = []
    for svc in services or []:
        name, gid = str(getattr(svc, "name", "") or "").lower(), str(getattr(svc, "guest", "") or "")
        r = mv._SERVICES.get(name)
        if name not in _ARR_API or r is None or not gid or getattr(svc, "vm", False):
            continue
        ver, endpoints = _ARR_API[name]
        cfg = "cat " + " ".join(r.paths) + " 2>/dev/null"
        key = f'$({cfg} | sed -n "s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p" | head -n 1)'
        port = f'$({cfg} | sed -n "s:.*<Port>\\(.*\\)</Port>.*:\\1:p" | head -n 1)'
        for ep in endpoints:
            # only each object's own top-level fields: a quality profile is tens of KB of nested qualities
            # and the read channel cuts it off mid-object (measured: not JSON past the first profile)
            inner = (f'curl -s -m 10 -H "X-Api-Key: {key}" "http://127.0.0.1:{port}/api/{ver}/{ep}" '
                     '| grep -E "^ {4}.(id|name|path).: "')     # no single quote: the read channel refuses `'\\''`
            out.append((name, ep, f"pct exec {gid} -- sh -c {mv._sq(inner)}"))
    return out


_FIELD_RE = re.compile(r'^\s*"(id|name|path)":\s*"?(.*?)"?,?\s*$')


def _summarise(body: str) -> str:
    """The top-level `id`/`name`/`path` lines, one object per `id` (the *arr apps write `id` last)."""
    rows: list[str] = []
    cur: dict[str, str] = {}
    for ln in str(body or "").splitlines():
        m = _FIELD_RE.match(ln)
        if not m:
            continue
        cur[m.group(1)] = m.group(2)
        if m.group(1) == "id":
            rows.append(", ".join(f"{k}={cur[k]!r}" for k in ("id", "name", "path") if k in cur))
            cur = {}
    return "; ".join(rows[:20])


async def read_status(spec, services: Optional[list]) -> list[dict]:
    """§17.1422 — `[{service, path, code, address}]`: what each probed path answers on the machine now."""
    from app.modules import service_truth as st
    out: list[dict] = []
    for name, path, cmd in status_reads(services):
        ok, body = await st._probe(spec, cmd)
        code = str(body or "").strip()[-3:] if ok else ""
        addr = next((getattr(s, "address", "") for s in services or []
                     if str(getattr(s, "name", "")).lower().startswith(name)), "")
        out.append({"service": name, "path": path, "code": code if code.isdigit() else "", "address": addr})
    return out


_INNER_TPL_RE = re.compile(r"(?:innerHTML|outerHTML|insertAdjacentHTML\([^,]*,)\s*\+?=?\s*`([^`]*)`", re.S)
_ATTR_INTERP_RE = re.compile(r"""=\s*(["'])[^"'`]*\$\{""")


def values_put_into_html_attributes(files: dict[str, str]) -> list[dict]:
    """§17.1424 — data interpolated into an HTML attribute inside an innerHTML template. A value holding a
    quote ends the attribute early: the input shows a cut value, and a form that saves what it shows writes
    the cut value back. Live, ADD126's page built `<input value="${value}">` for the Palworld settings, and
    nine of the live values carry double quotes (`ServerName = "Default Palworld Server"`, `BanListURL`, …):
    each would have shown empty, and Save would have written them back empty."""
    out: list[dict] = []
    for path, content in (files or {}).items():
        if not path.endswith((".html", ".htm", ".js", ".mjs")):
            continue
        for m in _INNER_TPL_RE.finditer(content or ""):
            hits = list(_ATTR_INTERP_RE.finditer(m.group(1)))
            if not hits:
                continue
            # name the one that loses data: a `value=` attribute is what a form saves back
            hit = next((h for h in hits if re.search(r"value\s*$", m.group(1)[:h.start()])), hits[0])
            line = m.group(1)[max(0, hit.start() - 40):hit.end() + 40].strip().replace("\n", " ")
            out.append({"command": f"{path}: {line[:120]}", "why": (
                f"this code puts data into an HTML attribute through an innerHTML template (`{line[:90]}`). A "
                f"value that holds a quote ends the attribute early -- the field shows a cut value, and a form "
                f"that saves what it shows writes the cut value back. The service's real values do hold quotes "
                f"(the Palworld settings: `ServerName = \"Default Palworld Server\"`, `BanListURL`, and seven "
                f"more). Build the element with document.createElement and set `.value` / `.textContent` "
                f"directly, never through markup.")})
            break
    return out


def a_secret_written_in_the_clear(files: dict[str, str], creds: Optional[dict[str, str]]) -> list[dict]:
    """§17.1427 — a literal value assigned to a secret the runner holds. Live, ADD127's unit file said
    `Environment=PANEL_PASSWORD=defrusciohomelab.duckdns.org`: the model, never shown the marker, invented
    the panel's password -- the public domain name -- and wrote it in the clear. Only the marker (filled at
    delivery) or a lookup (`process.env.X`, `$X`) may stand where the value goes."""
    names = [n for n, r in (creds or {}).items() if r == RUNNER_HELD]
    out: list[dict] = []
    for name in names:
        lit = re.compile(rf"\b{re.escape(name)}\s*[\"']?\s*[=:]\s*[\"']?(?!@@SCAFFOLD:|\$|process\.env)([^\s\"',;}}]+)")
        for path, content in (files or {}).items():
            m = lit.search(content or "")
            if not m:
                continue
            out.append({"command": f"{path}: {name}=…", "why": (
                f"`{path}` assigns a literal value to {name}, a secret the RUNNER holds: a value written here is "
                f"invented, and it sits in the file in the clear. Put `{SECRET_MARK.format(name=name)}` where "
                f"the value goes -- the delivery fills it from the runner's store on the machine, and the "
                f"engine never sees it -- e.g. `Environment={name}={SECRET_MARK.format(name=name)}` in a unit "
                f"file, then read `process.env.{name}`.")})
            break
    return out


def calls_a_dead_path(files: dict[str, str], status: Optional[list[dict]]) -> list[dict]:
    """§17.1422 — the version's code calls a path the engine MEASURED as not answering. Live, ADD125: told
    `/api/stats/summary` → 200 and `/admin/api.php?summary` → 400 (Pi-hole v6.4.3), the model still wrote
    the v5 path -- its step text asked for an "API token", a v5 notion -- and the sandbox, which reaches no
    machine, passed the route because it answered at all."""
    alive = [s for s in status or [] if s.get("code", "").startswith("2")]
    out: list[dict] = []
    for s in status or []:
        code = s.get("code", "")
        if not code or code.startswith("2"):
            continue
        base = s["path"].split("?", 1)[0]
        hit = next((p for p, c in (files or {}).items() if base in (c or "")), None)
        if hit is None:
            continue
        better = [a["path"] for a in alive if a["service"] == s["service"]]
        where = f" ({s['address']})" if s.get("address") else ""
        out.append({"command": f"{hit}: {base}", "why": (
            f"this code calls `{base}` on {s['service']}{where}, "
            f"and the engine asked that path just now: it answers HTTP {code}, so the feature cannot work. "
            + (f"What does answer: {', '.join(f'`{b}` (HTTP 2xx, no key)' for b in better)} -- use that, and "
               f"its own response fields." if better else "Use a path the service actually serves."))})
    return out


async def read_apis(spec, services: Optional[list], status: Optional[list[dict]] = None) -> str:
    """§17.1416 — what the services the code calls hold NOW, for the prompt. The model is handed the
    service's own files (§17.1412) but never the state of the APIs its code talks to, so it invented the
    values those APIs hold. Read like the files: read-only, off the machine, just now."""
    from app.modules import service_truth as st
    lines: list[str] = []
    for name, ep, cmd in api_reads(services):
        ok, body = await st._probe(spec, cmd)
        got = _summarise(body) if ok else ""
        addr = next((getattr(s, "address", "") for s in services or [] if str(getattr(s, "name", "")).lower() == name), "")
        lines.append(f"- {name}{f' ({addr})' if addr else ''} /api/{_ARR_API[name][0]}/{ep}: "
                     + (got or "(could not be read just now)"))
    for s in (status if status is not None else await read_status(spec, services)):   # §17.1421
        where = f" ({s['address']})" if s.get("address") else ""
        lines.append(f"- {s['service']}{where} GET {s['path']} without a key: "
                     + (f"HTTP {s['code']}" if s.get("code") else "(could not be read just now)"))
    if not lines:
        return ""
    return ("THE APIS YOUR CODE CALLS, AS THEY ARE NOW (read off the machine just now). Where your code needs one of "
            "these values -- a root folder, a quality profile -- use what the service holds (read it from the API at "
            "run time, or use these); never invent a default:\n" + "\n".join(lines))


FILES_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {"type": "array", "items": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}},
        "summary": {"type": "string"},
    },
    "required": ["files"],
}

SYSTEM = (
    "You develop software for one service on one machine. You are given the service's CURRENT files, read off "
    "the machine just now, and the facts the engine measured. Return the COMPLETE new content of every file you "
    "change or add -- whole files, never patches or shell commands. The engine delivers them itself: it backs up "
    "the directory, writes your files, restarts the service's unit and runs the step's checks. Keep every working "
    "part of the existing files; change what the step needs. Paths are absolute and must stay under the service's "
    "own directory (or a directory the step names). Nothing you write runs on the Proxmox host: your code runs "
    "inside the service's machine, as the service's user."
)


def test_contract(target: Optional[dict]) -> str:
    """§17.1413b — the acceptance test, stated up front. Live: six rounds stalled at one contract
    mismatch (GET answered `{"settings": {...}}`, PUT took the body as the map itself) because the
    model was never told the test is "GET, then PUT exactly that body back"."""
    if not target:
        return ""
    return (f"THE TEST THE ENGINE RUNS ON EVERY VERSION (in a sandbox, on copies of the real files):\n"
            f"1. GET {target['get']}  -> body B (JSON)\n"
            f"2. PUT {target['put']}  with body B EXACTLY, `Content-Type: application/json` -- a save that "
            f"changes nothing\n"
            f"3. {target['config']} must then say the same thing as before: the same `[section]` header, the "
            f"same keys, the same values (while that file has no settings line, the server runs on "
            f"{target.get('baseline') or 'its template'} -- that is \"before\").\n"
            f"So the PUT must accept exactly the shape the GET returns. A real client reads, edits one value "
            f"and sends the whole thing back the same way.")


def build_prompt(node: dict, host: Host, facts: str, workspace: dict[str, str],
                 current: Optional[dict[str, str]], evidence: str, round_no: int, kit: str = "",
                 test: str = "") -> str:
    parts = [f"STEP {node.get('node_key')}: {node.get('title')}", str(node.get("description") or ""), "",
             f"THE SERVICE: {host.name or host.unit} on guest {host.guest} ({'VM' if host.vm else 'container'}), "
             f"directory {host.workdir}, unit {host.unit}" + (f", runs as {host.user}" if host.user else ", runs as root"),
             "", facts.strip(), "", kit.strip(), "", test.strip(), "", "=== THE SERVICE'S CURRENT FILES (on the machine now) ==="]
    for p, c in workspace.items():
        parts += [f"--- {p} ---", c]
    if current:
        parts += ["", f"=== YOUR PREVIOUS VERSION (round {round_no - 1}) — start from it, keep what worked ==="]
        for p, c in current.items():
            parts += [f"--- {p} ---", c]
    if evidence:
        parts += ["", "=== WHAT HAPPENED WHEN THE ENGINE RAN IT ===", evidence]
    parts += ["", "Return {\"files\": [{\"path\": …, \"content\": …}], \"summary\": …} with every file you change "
              "or add, complete."]
    return "\n".join(parts)


async def propose(prompt: str) -> tuple[Optional[dict[str, str]], str]:
    """One version from the model: `{path: content}`, or None with the reason."""
    from app import model_router
    try:
        parsed, resp = await model_router.generate_json(prompt, FILES_SCHEMA, role="model_general", system=SYSTEM,
                                                        temperature=0.2, max_tokens=16000, think=False)
    except Exception as exc:
        return None, f"the model call failed: {exc!r}"
    # §17.1417 — only an answer that is WHOLE, valid JSON. Live, ADD123: the model wrote `"movie"`
    # unescaped inside a file's content; the lenient repair kept the text before the break and dropped
    # the rest -- the route file cut off mid-line and server.js gone -- and that fragment passed every gate.
    raw = _raw_text(resp)
    if raw:
        import json as _json
        try:
            _json.loads(_strip_fence(raw))
        except ValueError as exc:
            at = getattr(exc, "pos", None)
            near = _strip_fence(raw)[max(0, (at or 0) - 80):(at or 0) + 40] if at is not None else ""
            return None, (f"your answer was not valid JSON ({exc}), so no file in it can be trusted whole. Near "
                          f"the break: `{near}`. Inside a file's \"content\" string every double quote must be "
                          f"escaped as \\\" and every newline as \\n -- e.g. 'Type must be \\\"movie\\\"'.")
    files = (parsed or {}).get("files") if isinstance(parsed, dict) else None
    if not isinstance(files, list) or not files:
        return None, "the model returned no files"
    out = {str(f.get("path") or ""): str(f.get("content") or "") for f in files if isinstance(f, dict)}
    return out, str((parsed or {}).get("summary") or "")


def _raw_text(resp) -> str:
    for attr in ("text", "content", "response_content"):
        v = getattr(resp, attr, None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if isinstance(resp, str):
        return resp.strip()
    return ""


def _strip_fence(t: str) -> str:
    """The answer's own JSON object: inside a ``` fence, or the outermost `{…}` of prose around it."""
    t = t.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", t, re.S)
    t = m.group(1).strip() if m else t
    if not t.startswith("{") and "{" in t and "}" in t:
        t = t[t.index("{"):t.rindex("}") + 1]
    return t


def score(frame: dict, report: Optional[dict]) -> int:
    """Lower is closer. A clean frame is 0; a rehearsal-only refusal is the rehearsal's own score;
    any other refusal ranks behind every rehearsal result."""
    from app.modules import rehearsal
    refs = frame.get("refused") or []
    if frame.get("commands") and not refs:
        return 0
    if refs and all(rehearsal.MARK in str(r.get("why") or "") for r in refs):
        return rehearsal.score(report)
    return 20_000 + 100 * len(refs)


def evidence_of(frame: dict) -> str:
    return "\n".join(f"- {' '.join(str(r.get('why') or '').split())[:1500]}" for r in frame.get("refused") or [])
