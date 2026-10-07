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
    ok, listing = await st._probe(spec, f"{st.in_guest(host.guest, host.vm)} find {host.workdir} -maxdepth 3 -type f -size -256k")
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


def done_checks(node: Optional[dict], host: Host) -> list[str]:
    """The step's own checks (backticked `pct exec N -- …` in its text), then the unit is active."""
    checks = [c.strip() for c in _CHECK_RE.findall(str((node or {}).get("description") or ""))]
    tool = f"qm guest exec {host.guest} --" if host.vm else f"pct exec {host.guest} --"
    checks.append(f"{tool} systemctl is-active {host.unit}")
    return list(dict.fromkeys(checks))


def render_delivery(node: Optional[dict], host: Host, files: dict[str, str], checks: list[str]) -> str:
    """The delivery runbook -- a shape the engine owns: stage, back up, push, restart, check."""
    nk = re.sub(r"[^A-Za-z0-9_-]", "", str((node or {}).get("node_key") or "step")) or "step"
    stage = f"{STAGE_ROOT}/{nk}"
    wd_rel = host.workdir.lstrip("/")
    visible: dict[str, str] = {}
    carried: dict[str, str] = {}
    for path, content in files.items():
        (carried if _UNSAFE_RE.search(content or "") else visible)[path] = content or ""
    sh: list[str] = ["#!/usr/bin/env bash",
                     "# §17.1412 — delivered by the engine's own template: back up, push, restart, check.",
                     "set -euo pipefail",
                     f"STAGE={stage}"]
    for path, content in carried.items():
        b64 = base64.b64encode(content.encode()).decode()
        sh.append(f"mkdir -p {shlex.quote(os.path.dirname(stage + path))} && "
                  f"echo {b64} | base64 -d > {shlex.quote(stage + path)}")
    if host.vm:
        g = f"qm guest exec {host.guest}"
        sh += [f"qm status {host.guest} | grep -q running || qm start {host.guest}",
               f"BACKUP=/root/scaffold-backup-{nk}-$(date +%Y%m%d%H%M%S).tgz",
               f"{g} -- tar czf \"$BACKUP\" --exclude=node_modules -C / {shlex.quote(wd_rel)} >/dev/null"]
        for path in files:
            sh.append(f"{g} -- mkdir -p {shlex.quote(os.path.dirname(path))} >/dev/null")
            sh.append(f"{g} --pass-stdin 1 -- sh -c {shlex.quote('cat > ' + shlex.quote(path))} "
                      f"< \"$STAGE{path}\" >/dev/null")
        own = f"{g} --"
    else:
        g = f"pct exec {host.guest} --"
        sh += [f"pct status {host.guest} | grep -q running || pct start {host.guest}",
               f"BACKUP=/root/scaffold-backup-{nk}-$(date +%Y%m%d%H%M%S).tgz",
               f"{g} tar czf \"$BACKUP\" --exclude=node_modules -C / {shlex.quote(wd_rel)}"]
        for path in files:
            sh.append(f"{g} mkdir -p {shlex.quote(os.path.dirname(path))}")
            sh.append(f"pct push {host.guest} \"$STAGE{path}\" {shlex.quote(path)}")
        own = g
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
        out += [f"### {stage}{path}", "```", content.rstrip("\n"), "```", ""]
    out += [f"### {stage}/deliver.sh", "```bash", "\n".join(sh), "```", ""]
    if carried:
        out += [f"({len(carried)} file(s) carried inside deliver.sh base64-encoded, because their text holds "
                f"a fence or a heading line: {', '.join(carried)})", ""]
    out += ["## Run this", "", "```bash", f"bash {stage}/deliver.sh", "```", "",
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


def build_prompt(node: dict, host: Host, facts: str, workspace: dict[str, str],
                 current: Optional[dict[str, str]], evidence: str, round_no: int, kit: str = "") -> str:
    parts = [f"STEP {node.get('node_key')}: {node.get('title')}", str(node.get("description") or ""), "",
             f"THE SERVICE: {host.name or host.unit} on guest {host.guest} ({'VM' if host.vm else 'container'}), "
             f"directory {host.workdir}, unit {host.unit}" + (f", runs as {host.user}" if host.user else ", runs as root"),
             "", facts.strip(), "", kit.strip(), "", "=== THE SERVICE'S CURRENT FILES (on the machine now) ==="]
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
    files = (parsed or {}).get("files") if isinstance(parsed, dict) else None
    if not isinstance(files, list) or not files:
        return None, "the model returned no files"
    out = {str(f.get("path") or ""): str(f.get("content") or "") for f in files if isinstance(f, dict)}
    return out, str((parsed or {}).get("summary") or "")


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
