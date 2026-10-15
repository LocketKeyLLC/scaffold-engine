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
from itertools import zip_longest
import shlex
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("scaffold.develop")

#: versions the model may write before the best one is shown
ROUNDS = int(os.environ.get("DEVELOP_ROUNDS", "8"))
#: where the delivery stages files on the host before pushing them into the guest
STAGE_ROOT = "/tmp/scaffold-dev"

_SKIP_RE = re.compile(r"/node_modules/|/\.git/|/(?:venv|\.venv|site-packages|__pycache__|dist|build)/|\.bak(?:\.|$)|\.log$|/package-lock\.json$|\.tgz$")
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
    # §17.1544 — sibling app directories the brief names (e.g. a Vite UI at
    # /opt/control-panel-ui beside the backend's WorkingDirectory /opt/control-panel-backend). The
    # app spans more than the unit's one WorkingDirectory; read_workspace reads these too and
    # allowed_path permits writes into them, so a UI restyle is not misplaced in the backend.
    extra_dirs: tuple = ()


def _text(node: Optional[dict]) -> str:
    # §17.1550 — all the step's words, prompt_template included (as the delivery-path assembly at the
    # bottom of this module already does). `credentials_for` gates runner-secret INJECTION on the secret
    # name appearing here, `host_for` decides build-intent from here, and `engine_doc`/path-extraction read
    # here too; a step the planner wrote into prompt_template (or an operator correction added there by
    # add_review) was invisible to every one of them. Live: an operator correction naming the panel's held
    # engine key sat in prompt_template, so the delivery would not have injected it.
    return " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))


def _step_text(node: Optional[dict]) -> str:
    """§17.1550 — the step's text is ``prompt_template``, or ``description`` when that is empty
    (``plan_reconcile.STEP_TEXT_EXPR`` — the same rule every other read of a step's text already
    follows). The develop path read ``description`` alone, so a step the planner wrote whole into
    ``prompt_template`` reached the model as its title only, and an operator's redraft review
    (``decision_pause.add_review`` writes it to whichever column already holds the text) never
    arrived — the model redrafted from the title and reproduced the same block.

    Live, 5-section control-panel job c28ddeb0, 2026-10-14: T19 "Write Scaffold UI section" carried
    its whole spec AND a redraft correction in ``prompt_template`` (860 chars) while ``description``
    was empty, so every round reinvented a generic HTTP-request builder from the title and ignored
    the correction. The regular executor's ``build_base_prompt`` already folds both columns in
    (§17.1237/ADD100); this is the sibling call site that drifted."""
    return str((node or {}).get("prompt_template") or "").strip() or str((node or {}).get("description") or "")


_HOST_CHANGE_RE = re.compile(r"\b(?:pct|qm)\s+(?:set|reboot|start|stop|shutdown|create|destroy|resize|migrate|restore)\b"
                             r"|\b(?:ifreload|ifup|ifdown)\b|/etc/network/interfaces")


_CHECKS_OR_WRITES_UP_RE = re.compile(r"\s*(?:validate|verify|check|test|audit|review|document)\b", re.I)


def host_for(node: Optional[dict], services: Optional[list], brief: str = "") -> Optional[Host]:
    """The step builds software, and a measured service with a directory hosts it -- or None.

    The service is the one on the step's own guest (`LXC 111: …`), else one the step names,
    else -- §17.1537 -- the single developable service the JOB BRIEF names as its build target."""
    from app.modules.step_decomposition import builds_a_capability
    text = _text(node)
    # §17.1537 — a node whose TOOL is CodeGen explicitly builds code; the text heuristic
    # `builds_a_capability` misses generic titles ("Create CSS variables", "Restyle UI
    # components"), so such a node was never developed and its generated files never deployed.
    # The tool is ground truth; the later guards (checks/writes-up, host-level change, named-files)
    # still keep a CodeGen node that is really a check or a host change out of the develop flow.
    _is_codegen = str((node or {}).get("tool") or "").strip().lower() in ("codegen", "code")
    if not builds_a_capability(text) and not _is_codegen:
        return None
    # §17.1454 — a step that checks or writes up the build does not build software. Live (T37 "Validate
    # entire build", 2026-10-10): its text names the control-panel backend it checks, so it was developed,
    # refused for having no feature check of its own, and parked with nothing to run.
    if _CHECKS_OR_WRITES_UP_RE.match(str((node or {}).get("title") or "")):
        return None
    # §17.1431 — a step whose own text names a HOST-level change is not developed: the delivery carries
    # files into one service, never `pct set` / a reboot. Live, ADD128 ("CT 120: fix its DNS
    # (`pct set 120 --nameserver …`) and serve the panel") was routed here and returned no files 8 rounds.
    if _HOST_CHANGE_RE.search(" ".join(re.findall(r"`([^`]+)`", text))):
        return None
    m = re.search(r"\b(?:LXC|CT|VM|container|guest)\s*(\d{3,5})\b", str((node or {}).get("title") or ""), re.I)
    cands = [s for s in services or [] if getattr(s, "workdir", "") and getattr(s, "unit", "")]
    pick = None
    if m:
        pick = next((s for s in cands if str(s.guest) == m.group(1)), None)
        # §17.1454 — the title names the guest the work is on; a service elsewhere is not its host.
        # Live (ADD149 "CT 120 (Caddy): publish ONLY the control panel"): no measured service on 120 has a
        # directory, so the name fallback below picked RADARR (CT 103) because the text says "Radarr's 302",
        # and seven rounds proposed `/etc/caddy/Caddyfile` for the wrong container until each was refused.
        if pick is None:
            return None
    if pick is None:
        # §17.1581 — the step's own words place the work ON a guest ("… the Radarr handler on container 111"):
        # the developable service there hosts it, and a service the step merely NAMES elsewhere does not.
        # Live (c2792d4b T4): the title's name match hosted panel work on Radarr's CT 103.
        from app.modules.machine_truth import place_guest as _place_guest
        _pg = _place_guest(node)
        if _pg:
            pick = next((s for s in cands if str(s.guest) == _pg), None)
            if pick is None:
                return None
    # §17.1549 — a job that DECLARES a single deploy target (deploy-verb phrasing, §17.1540
    # `deploy_target_guest`) keeps EVERY build node on it. A panel-aggregator node names the
    # service it QUERIES, not the machine it deploys to: "Add Palworld status endpoint" is a
    # route on the control panel that reads VM 106 via the Proxmox API — it must NOT deploy a
    # status server ONTO VM 106. So the declared target outranks the name-match below; only an
    # explicit guest id in the title (handled above) overrides it. Live (job 5b71ec89,
    # 2026-10-14): that very node matched the palworld service on VM 106, delivered
    # `/opt/palworld/status_server.py` there and restarted the game, instead of a CT 111 route.
    if pick is None and str(brief or ""):
        from app.modules.step_classify import deploy_target_guest as _deploy_target
        _tgt = _deploy_target(str(brief))
        if _tgt:
            pick = next((s for s in cands if str(s.guest) == _tgt), None)
    if pick is None:
        # §17.1456 — the host is the step's SUBJECT, which its title names. Live (ADD151 "Proxmox firewall:
        # enforce the security groups on every guest", 2026-10-10): the description lists Jellyfin's ports, so
        # the name match over the whole text hosted a firewall step on Jellyfin's container as a software build.
        title = str((node or {}).get("title") or "")
        low = title.lower()
        pick = next((s for s in cands if str(getattr(s, "name", "")).lower() in low
                     or str(s.workdir) in title), None)
    if pick is None and str(brief or ""):
        # §17.1537 — a build node that names no host of its OWN (a CodeGen title like "Create CSS
        # variables" names neither the guest nor the service) still deploys to the JOB's build
        # target: the single developable service the brief names, by its unit, its container id, or
        # its workdir. Without this the files are generated and verified but never written to the
        # machine. Live (control-panel job ae721781, 2026-10-13): every CodeGen node's host_for
        # returned None, so the dark-theme/health-dashboard code was produced and passed
        # verification while CT 111 was never touched. UNAMBIGUOUS only — exactly one candidate the
        # brief names — so a job that touches two services never deploys one step to the wrong one.
        b = str(brief)
        named = [s for s in cands
                 if (getattr(s, "unit", "") and str(s.unit) in b)
                 or re.search(rf"\b(?:LXC|CT|VM|container|guest)\s*{re.escape(str(s.guest))}\b", b, re.I)
                 or (getattr(s, "workdir", "") and str(s.workdir) in b)]
        if len(named) == 1:
            pick = named[0]
    if pick is None:
        return None
    # §17.1465 — a step whose named files all lie outside the service's directory is configuring the
    # GUEST, not building the service. Live (ADD154 "VM 106 (palworld-server): take its address from the
    # router (DHCP)…"): its title names VM 106, the Palworld service there has a workdir, so the netplan
    # change was "developed" in the game server's directory and refused for eight rounds.
    _wd = str(pick.workdir).rstrip("/")
    # §17.1544 — sibling app directories the brief/step names beside the service's WorkingDirectory:
    # the app can span more than the unit's one dir (a Vite UI at /opt/control-panel-ui beside the
    # backend's /opt/control-panel-backend). Include a same-parent dir whose basename shares the
    # workdir's prefix (control-panel-*), so the UI code is read and its files may be written —
    # otherwise a UI change is confined to the backend workdir and misplaced there. Computed BEFORE
    # the §17.1465 guard so a step whose only named path is the sibling UI dir counts as in-app.
    _parent, _base = _wd.rsplit("/", 1) if "/" in _wd else ("", _wd)
    _prefix = _base.rsplit("-", 1)[0] if "-" in _base else _base
    _hay = str(brief or "") + " " + text
    extra = tuple(d for d in dict.fromkeys(re.findall(r"/opt/[A-Za-z0-9][\w-]*", _hay))
                  if d.rstrip("/") != _wd and d.rsplit("/", 1)[0] == _parent
                  and d.rsplit("/", 1)[-1].startswith(_prefix)
                  and not re.search(r"venv|node_modules|cache|\.git|dist|build", d))
    # §17.1465 — a step whose named files all lie outside the app's directories is configuring the
    # GUEST, not building the service. A path under the WorkingDirectory OR a sibling app dir (§1544)
    # is in-app; anything else (a netplan under /etc, another service's /opt) is not.
    _in_app = (_wd, *(d.rstrip("/") for d in extra))
    named = [p for p in re.findall(r"(?<![\w.~-])(/(?:etc|usr|var|lib|boot|root|home|srv|opt)/[\w./-]+)", text)
             if not p.startswith("/etc/systemd/system/")]
    if named and not any(p.startswith(d + "/") or p == d for p in named for d in _in_app):
        return None
    return Host(guest=str(pick.guest), vm=bool(getattr(pick, "vm", False)), workdir=str(pick.workdir).rstrip("/"),
                unit=str(pick.unit), user=str(getattr(pick, "user", "") or ""),
                group=str(getattr(pick, "group", "") or ""), name=str(getattr(pick, "name", "") or ""),
                extra_dirs=extra)


def allowed_path(path: str, host: Host, node: Optional[dict]) -> bool:
    """A file the model may write: under the service's directory, under a directory the step
    itself names, or the service's own unit file. Absolute, no `..`."""
    p = str(path or "")
    if not p.startswith("/") or "/../" in p or p.endswith("/") or "\x00" in p:
        return False
    if p.startswith(host.workdir + "/") or p == f"/etc/systemd/system/{host.unit}":
        return True
    if any(p.startswith(d.rstrip("/") + "/") for d in host.extra_dirs):  # §17.1544 — a sibling app dir
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
    out: dict[str, str] = {}
    per_dir: list[list[str]] = []
    for d in (host.workdir, *host.extra_dirs):  # §17.1544 — the WorkingDirectory AND sibling app dirs
        d = d.rstrip("/")
        ok, listing = await st._probe(spec, rehearsal.listing_command(st.in_guest(host.guest, host.vm), d))
        if ok:
            per_dir.append([p.strip() for p in (listing or "").split("\n")
                            if p.strip().startswith(d + "/") and not p.strip().endswith("/")
                            and not _SKIP_RE.search(p.strip())])
    # §17.1544 — round-robin across the dirs so the MAX_SEED_FILES budget is not spent entirely on the
    # first (backend) dir, leaving a sibling UI dir unread; each dir is represented.
    paths = [p for group in zip_longest(*per_dir) for p in group if p]
    for p in list(dict.fromkeys(paths))[:rehearsal.MAX_SEED_FILES]:
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


#: §17.1582 — a GET route the service's own code registers: `app.get('/api/x', …)` / `router.get("/x", …)`
_ROUTE_DEF_RE = re.compile(r"\b(?:app|router)\.get\(\s*['\"](/[^'\"\s]*)['\"]")
#: words of a fix step's title that say nothing about WHICH feature
_FEATURE_STOP = frozenset({
    "fix", "the", "and", "for", "its", "endpoint", "endpoints", "backend", "backends", "handler", "handlers",
    "remaining", "any", "not", "covered", "container", "edit", "return", "returns", "real", "data", "clear", "json",
    "error", "errors", "preserve", "existing", "feature", "features", "every", "each", "service", "services", "route",
    "routes", "api", "with", "from", "that", "this", "all", "other", "make", "add", "update", "repair", "wire", "page",
})
_UI_WORDS = frozenset({"ui", "frontend", "page", "html", "index", "render", "rendering", "section", "sections",
                       "fetch", "loading", "css", "theme", "dom", "browser"})


#: §17.1584 — name words every route file has: a step saying "engine-held secrets" is not about scaffold_engine_route.js
_GENERIC_STEMS = frozenset({"server", "index", "main", "app", "api", "engine", "route", "routes", "view", "ui", "status",
                            "request", "requests", "library", "backend", "dashboard", "settings", "health", "handler", "controller"})


def derived_from_files(host: Host, files: Optional[dict[str, str]], port: str) -> list[str]:
    """§17.1584 — the feature check of a DELIVERY: the GET routes defined in the files it rewrites.

    Live (c2792d4b T4): the word-derived permit (§17.1582) named five routes whose handlers mention Radarr —
    /api/overview among them, which hangs for a reason of its own — so this step's verify would have failed on
    another step's bug. The files the model changed (media_library_route.js) say exactly which routes this
    delivery touches; those are what its checks read. Empty when the changed files define no route (a UI
    file): the caller keeps the word-derived checks."""
    if not port:
        return []
    tool = f"qm guest exec {host.guest} --" if host.vm else f"pct exec {host.guest} --"
    base = f"{tool} curl -s --fail-with-body --max-time 20 http://127.0.0.1:{port}"
    routes: list[str] = []
    for path, body in sorted((files or {}).items()):
        if "node_modules" in path or "scaffold-kit" in path or not path.endswith((".js", ".ts", ".mjs", ".py")):
            continue
        routes += _ROUTE_DEF_RE.findall(str(body or ""))
    return [f"{base}{r}" for r in dict.fromkeys(routes)]


def service_port(host: Host, services: Optional[list]) -> str:
    """§17.1582 — the measured loopback port of the service `host` develops on, or ''."""
    for s in services or []:
        if str(getattr(s, "unit", "") or "") == str(host.unit) and str(getattr(s, "guest", "") or "") == str(host.guest):
            ports = [str(p) for p in (getattr(s, "ports", ()) or ()) if str(p).strip()]
            if ports:
                return ports[0]
    return ""


def derived_checks(node: Optional[dict], host: Host, workspace: Optional[dict[str, str]], port: str) -> list[str]:
    """§17.1582 — the feature check a fix step's text never named, read off the SERVICE'S OWN ROUTES.

    Live (c2792d4b T4 "Fix Radarr endpoint backend — edit the Radarr handler on container 111 … return
    JSON data"): the step names no backticked check, so §17.1418 refused it with "its text must name the
    check … e.g. `pct exec 111 -- curl … /api/<route>`" — and the engine had the service's code in hand
    (`read_workspace`) and the panel's probe one step upstream. The routes whose handler FILE carries a
    word of the step's title ("radarr" → media_library_route.js → `/api/media-library`; "Palworld, media,
    network, FlareSolverr" → theirs) are the step's feature; a UI step ("fetch", "rendering", "sections")
    has the served page. Each check is a plain `curl -s --fail-with-body` of the loopback port — a route
    that hangs or errors fails it — and §17.1548 presents the held login to it when the service has one.
    Nothing derived (no port, no words, no route file) leaves §17.1418's refusal standing."""
    if not port or not workspace:
        return []
    title = str((node or {}).get("title") or "")
    full = (title + " " + _step_text(node)).lower()
    words = {w for w in re.findall(r"[a-z][a-z0-9]{2,}", title.lower())} - _FEATURE_STOP
    try:
        from app.modules.service_truth import names_in
        words |= {n.lower() for n in names_in(node)} | {n.lower() for n in names_in({"title": full})}   # the body's services too
    except Exception:
        pass
    tool = f"qm guest exec {host.guest} --" if host.vm else f"pct exec {host.guest} --"
    base = f"{tool} curl -s --fail-with-body --max-time 20 http://127.0.0.1:{port}"
    feature_words = words - _UI_WORDS
    step_words = set(re.findall(r"[a-z][a-z0-9]{2,}", full)) - _FEATURE_STOP - _UI_WORDS
    routes: list[str] = []
    for path, body in sorted((workspace or {}).items()):
        if "node_modules" in path or not path.endswith((".js", ".ts", ".mjs", ".py")):
            continue
        found = _ROUTE_DEF_RE.findall(str(body or ""))
        if not found:
            continue
        low, fname = str(body or "").lower(), path.rsplit("/", 1)[-1].lower()
        # the title's words (and the services the step names) in the file — or, the other way round, the file's
        # OWN name words in the step's text ("… Palworld, media, network, FlareSolverr" → network_view_route.js)
        stem_words = set(re.findall(r"[a-z][a-z0-9]{2,}", fname.rsplit(".", 1)[0])) - _FEATURE_STOP - _GENERIC_STEMS
        if any(w in fname or w in low for w in feature_words) or (stem_words and stem_words & step_words):
            routes += found
    out = [f"{base}{r}" for r in dict.fromkeys(routes)]
    if not out and words & _UI_WORDS and any(p.endswith((".html", ".htm")) for p in workspace):
        out = [f"{base}/"]                      # the served page is the UI step's feature; its behaviour is the verify step's
    return out


def done_checks(node: Optional[dict], host: Host, derived: Optional[list[str]] = None) -> list[str]:
    """The step's own READ checks (backticked `pct exec N -- …` in its text), then the ones the engine
    derived from the service's routes (§17.1582), then the unit is active.

    §17.1414 — only reads: the verify channel carries nothing else, and silently dropped ADD123's
    POST. A check that WRITES is the run's own acceptance step (`acceptance_checks`)."""
    checks = [c.strip() for c in _CHECK_RE.findall(_step_text(node))   # §17.1550 — whole step text
              if _is_read(c.strip()) and not _needs_a_secret(c.strip())]
    checks += [str(c) for c in derived or []]
    tool = f"qm guest exec {host.guest} --" if host.vm else f"pct exec {host.guest} --"
    checks.append(f"{tool} systemctl is-active {host.unit}")
    return list(dict.fromkeys(checks))


NO_OWN_CHECK = "names no command the engine can run to see the feature work"


def own_check_refusal(node: Optional[dict], host: Host, derived: Optional[list[str]] = None) -> list[dict]:
    """§17.1418 — a developed step needs a check of ITS feature. `done_checks` always adds the unit's
    `is-active`, which the delivery itself makes true; a step whose text names nothing else would be
    recorded done while its route answers a connection error. Live: ADD124 ("returns the engine's
    /health with status healthy", in prose) was offered with `is-active` as its only check.

    §17.1543 — the concern is a step whose ONLY proof is the unit's `is-active`, which the delivery
    fakes by restarting it. A CodeGen node delivers FILE CONTENT: its proof is the delivered file
    (the develop flow's own content gates judge it — §17.1414 dead-path / cleartext-secret /
    html-attribute), and a UI build carries its own downstream verify nodes; a Vite-UI write does not
    even restart the step's unit. So a CodeGen build node is exempt — refusing it for lacking an
    `is-active`-style check parked every one with nothing to run (live: control-panel, T9/T12/…)."""
    if str((node or {}).get("tool") or "").strip().lower() == "codegen":
        return []
    if acceptance_checks(node) or len(done_checks(node, host, derived)) > 1:   # §17.1582 — a derived check counts
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
    return list(dict.fromkeys(c.strip() for c in _CHECK_RE.findall(_step_text(node))))   # §17.1550


def acceptance_checks(node: Optional[dict]) -> list[str]:
    """§17.1414 — the step's own checks that WRITE (ADD123: a POST that asks Radarr for a film). They
    cannot be verified read-only, and the sandbox cannot reach another machine's API, so they run as
    the approved block's LAST step -- visible to the operator before approval -- with `curl -f`, so
    an HTTP error fails the run instead of printing an error page and exiting 0."""
    out: list[str] = []
    for c in _CHECK_RE.findall(_step_text(node)):   # §17.1550 — whole step text
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
        # §17.1496 — the staged copy is about to hold the real key: owner-only BEFORE the fill, not
        # only on the guest's copy after the push. /tmp is sticky, not private, and a SIGKILL skips the trap.
        sh.append("chmod 600 " + " ".join(shlex.quote(staged(p)) for p in secret_files))
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
            # §17.1585 — a value the MACHINE holds wins over a copy the runner stores: live (c2792d4b T4), the step
            # named RADARR_API_KEY, the runner's copy replaced Radarr's own key as the read, and the delivered
            # panel answered "Failed to fetch Radarr library: HTTP 401" exactly as before — the stored copy was
            # stale while `read_apis` had just read Radarr's API with the key off its config.xml.
            out.setdefault(str(n), RUNNER_HELD)
    return out


#: §17.1583 — a credential a workspace file carries in the clear: `"sonarrApiKey": "<hex>"`, `apiKey = '<hex>'`,
#: `SONARR_API_KEY=<hex>`. The key's letters name the credential (sonarrApiKey ≡ SONARR_API_KEY).
_FILE_CREDENTIAL_RE = re.compile(
    r"(?P<pre>(?P<q>[\"']?)(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)(?P=q)\s*[:=]\s*(?P<vq>[\"']?))(?P<val>[A-Za-z0-9_-]{16,128})(?=[\"'\s,;}\]]|$)")


def _cred_norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def mark_held_credentials(workspace: dict[str, str], creds: dict[str, str],
                          held: Optional[list] = None) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """§17.1583 — a credential the SERVICE'S OWN FILES carry in the clear reaches the model as its mark, not
    its value, and comes back as its value at delivery.

    Live (c2792d4b T4): `read_workspace` handed the model the panel's config/media-request.json with Sonarr's
    key in it; the model wrote the file back whole, key included, and the value sat in the frame the operator
    reads and in the record. The engine HOLDS that credential (`creds`: a machine read or a runner secret), and
    the delivery already fills `@@SCAFFOLD:NAME@@` from it — so the file the model sees carries the mark where
    the key's own name says which credential it is (`sonarrApiKey` ≡ `SONARR_API_KEY`). Returns the marked
    workspace and the names marked. A value whose key names no credential the engine holds is left as it is
    (and §17.1391's gate refuses it in a proposed file).

    §17.1585 — `held`: every runner secret's NAME, not only the step's `creds`. Live (c2792d4b T5): T4's delivery
    had written `Environment=RADARR_API_KEY=<key>` into the unit file; T5 names Sonarr, so RADARR_API_KEY was not
    in its creds, the unit reached the model with the key in the clear, and the model copied it back twice
    (§17.1391 refused; §17.1583 stalled). A held name found in the workspace is marked and joins `creds` as
    runner-held, so the delivery can fill it. Returns (workspace, marked names, creds)."""
    creds = dict(creds or {})
    names = set(creds) | {str(h) for h in held or [] if re.fullmatch(r"[A-Z][A-Z0-9_]{2,60}", str(h))}
    if not names or not workspace:
        return dict(workspace or {}), [], creds
    by_norm = {_cred_norm(n): n for n in sorted(names)}
    out: dict[str, str] = {}
    marked: set[str] = set()

    def _repl(m: "re.Match[str]") -> str:
        name = by_norm.get(_cred_norm(m.group("key")))
        if not name:
            return m.group(0)
        marked.add(name)
        return m.group("pre") + SECRET_MARK.format(name=name)
    for path, body in (workspace or {}).items():
        out[path] = _FILE_CREDENTIAL_RE.sub(_repl, str(body or "")) if path and not path.endswith((".js.map", ".lock")) else body
    for n in marked:
        creds.setdefault(n, RUNNER_HELD)          # a held name the files carry: the delivery fills it from the runner
    return out, sorted(marked), creds


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
    `Environment=PANEL_PASSWORD=homelab-demo.duckdns.org`: the model, never shown the marker, invented
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


def _split_top_args(s: str) -> list[str]:
    """Split a call's argument list on top-level commas (no nesting in the paths we parse)."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _balanced_arg(s: str, start: int) -> tuple[str, int]:
    """From just after an opening `(` at `start`, return (inner text, index past the matching `)`)."""
    depth, i = 1, start
    while i < len(s) and depth:
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
        i += 1
    return s[start:i - 1], i


def _resolve_served_dir(expr: str, base: str) -> Optional[str]:
    """Resolve a static-root expression to an absolute dir, relative to the backend file's dir (`base`)."""
    import posixpath
    expr = expr.strip()
    m = re.match(r"path\.(?:join|resolve)\((.+)\)$", expr, re.S)        # path.join(__dirname, '..', 'ui')
    if m:
        cur = None
        for seg in _split_top_args(m.group(1)):
            seg = seg.strip()
            if seg == "__dirname":
                cur = base
            elif seg[:1] in ("'", '"'):
                part = seg.strip("'\"")
                cur = posixpath.normpath(posixpath.join(cur or base, part)) if cur is not None else part
        return cur
    m = re.match(r"__dirname\s*\+\s*['\"]([^'\"]+)['\"]$", expr)         # __dirname + '/public'
    if m:
        return posixpath.normpath(posixpath.join(base, m.group(1).lstrip("/")))
    m = re.match(r"['\"]([^'\"]+)['\"]$", expr)                          # 'public' / '/abs/dir'
    if m:
        p = m.group(1)
        return p if p.startswith("/") else posixpath.normpath(posixpath.join(base, p))
    return None


def served_entries(workspace: dict[str, str]) -> list[str]:
    """§17.1546 — the file(s) the service SERVES to the browser, found from the backend's own static-file
    directive in the workspace it was read with. Live (control-panel ae721781): the backend does
    `express.static(path.join(__dirname, '..', 'control-panel-ui'))`, so the browser gets that dir's
    hand-written `index.html` -- NOT the React `src/` the restyle edited, nor the unused Vite `dist/`.
    Without naming the served file, a theme change landed on a source the service never serves and the
    panel stayed light. Covers Express static, an nginx/Caddy `root`, and a Flask/FastAPI static dir."""
    import posixpath
    dirs: list[str] = []
    for path, content in (workspace or {}).items():
        if not path.endswith((".js", ".ts", ".mjs", ".cjs", ".py", ".conf")):
            continue
        base = posixpath.dirname(path)
        for key in ("express.static(", "serveStatic("):      # nested parens: extract the balanced arg
            i = (content or "").find(key)
            while i >= 0:
                arg, j = _balanced_arg(content, i + len(key))
                d = _resolve_served_dir(arg.strip(), base)
                if d:
                    dirs.append(d.rstrip("/"))
                i = content.find(key, j)
        for m in re.finditer(r"(?:StaticFiles\(\s*directory\s*=|send_from_directory\(\s*)['\"]([^'\"]+)['\"]",
                             content or ""):
            d = m.group(1)
            dirs.append((d if d.startswith("/") else posixpath.normpath(posixpath.join(base, d))).rstrip("/"))
        for m in re.finditer(r"\broot\s+([^\s;*]+)\s*;", content or ""):   # nginx / Caddy root
            dirs.append(m.group(1).rstrip("/"))
    out: list[str] = []
    for d in dict.fromkeys(dirs):
        for p in workspace:                                   # the served dir's HTML the engine actually read
            if p.endswith((".html", ".htm")) and (posixpath.dirname(p) == d or p.startswith(d + "/")):
                out.append(p)
        entry = d + "/index.html"
        if entry not in out:
            out.append(entry)
    return list(dict.fromkeys(out))


def served_files_doc(served: list[str]) -> str:
    """§17.1546 — name the served file(s) in the develop prompt so a visual change lands where it shows."""
    if not served:
        return ""
    return ("WHAT THE SERVICE SERVES TO THE BROWSER (its static web root, from the backend's own static-file "
            "directive). A change the user SEES -- styling, theme, colour, layout, markup -- must be made IN these "
            "file(s); they are what the browser loads:\n" + "\n".join(f"- {p}" for p in served) +
            "\nA build SOURCE the service does not serve (for example a framework `src/` whose built bundle is not "
            "what this service returns) will NOT change the page, however correct it looks. If the served file holds "
            "its styles inline, change them there.")


_VISUAL_CHANGE_RE = re.compile(
    r"\b(re-?style|re-?skin|theme|dark[\s-]*mode|colou?r[\s-]*scheme|palette|stylesheet|styling|"
    r"\bcss\b|look[\s-]*and[\s-]*feel|appearance|visual|layout)\b", re.I)


def restyle_missed_the_served_file(node: Optional[dict], files: dict[str, str],
                                   served: list[str], workspace: dict[str, str]) -> list[dict]:
    """§17.1546 — a VISUAL change whose files the served page never loads will not be seen. Gate it: the
    served HTML (served AS-IS, not a build artifact) references none of the written files by name and the
    step edited none of the served file itself. Live: the dark-theme restyle wrote `src/App.css`, but the
    served `/opt/control-panel-ui/index.html` is a standalone page with INLINE styles that links nothing,
    so the panel stayed light. Conservative: it stays silent for a build-pipeline SPA (served file under
    dist/build/out -- there the source IS the right edit) and when the served page does load a written file."""
    if not served or not files:
        return []
    text = "\n".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not _VISUAL_CHANGE_RE.search(text):
        return []
    if any(p in served for p in files):                       # it edited the served file itself
        return []
    import posixpath
    served_html = [p for p in served if p.endswith((".html", ".htm"))
                   and not re.search(r"/(?:dist|build|out|\.next|\.output)/", p)]
    if not served_html:
        return []
    styled = [p for p in files if p.endswith((".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".js", ".ts", ".html"))]
    if not styled:
        return []
    for sh in served_html:
        html = (workspace or {}).get(sh, "")
        refs = {posixpath.basename(r.split("?", 1)[0])
                for r in re.findall(r'(?:href|src)\s*=\s*["\']([^"\']+)["\']', html)}
        if any(posixpath.basename(w) in refs for w in files):  # the served page DOES load one of them
            return []
    return [{"command": f"(wrote {', '.join(list(files)[:3])})", "why": (
        "this is a visual change, but the page the service serves loads NONE of the files it wrote, so the change "
        f"will not be visible. The service serves: {', '.join(served_html)} -- served as-is. Make the change there "
        "(if that file holds its styles inline, edit the inline styles), not in a source the served page does not load.")}]


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
    parts = [f"STEP {node.get('node_key')}: {node.get('title')}", _step_text(node), "",   # §17.1550
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


def stalled(previous: str, now: str) -> bool:
    """§17.1583 — a round whose refusals are exactly the previous round's brings the model nothing new."""
    p, n = " ".join(str(previous or "").split()), " ".join(str(now or "").split())
    return bool(n) and p == n


def nothing_refusal(evidence: str) -> list[dict]:
    """§17.1496 — the refusal a developed step parks with when no round produced a version to run.

    It used to park with no commands AND no refusal: "Run" greyed out and nothing to read, the
    per-round reasons (no files, every path outside the service directory, a refused shape)
    living only in log lines. The last round's reason is what the operator sees."""
    why = " ".join(str(evidence or "").split()).lstrip("- ").strip() or "every round came back with no files"
    return [{"command": "(develop)", "why": (
        f"{ROUNDS} rounds of drafting produced no version the engine could deliver. The last round: "
        f"{why[:600]}. Nothing ran. Redraft with a note that says what the version must contain, "
        f"or split the step.")}]
