"""§17.1641 — a step that checks a measured web service's API checks the routes the service's OWN code registers.

Live (job 5db1b088 T26 "Verify control panel API", 2026-10-18): the step asked for every /api route of the control
panel on CT 111 to answer without hanging. The model drafted a Python script that called five routes it made up
(`/api/palworld`, `/api/media`, `/api/scaffold`, `/api/network`, `/api/health`) and sent no login, on a panel whose
`server.js` puts `basicAuth` in front of every route and registers seventeen others (`/api/palworld/status`,
`/api/media-library`, …). Run, it would have reported five 401s -- and handed T28 "Fix control panel API" five routes
that are not broken. The engine already HOLDS both facts: the routes are in the service's files, which the develop
flow reads (`develop.read_workspace`), and the login is a stored secret §17.1548 presents on stdin.

So the check is the engine's, not the model's: one command, inside the service's guest, that calls each registered
GET route on its measured port with the held login, prints `PASS|FAIL <code> <seconds> <route>` per route, and exits
non-zero when any route fails or hangs. One command, because `run_block` stops at the first failure and every route
must be reported.
"""
from __future__ import annotations

import re
from typing import Optional

#: what a step must ask about for this check to be its answer
_API_WORDS_RE = re.compile(r"\b(?:api|apis|routes?|endpoints?)\b", re.I)
#: a GET route the service registers: `app.get('/api/x', …)` / `router.get("/api/x", …)`; a `:param` route is not called
_ROUTE_RE = re.compile(r"\b(?:app|router)\.get\(\s*['\"](/api/[^'\"\s:*]*)['\"]")
#: the per-route budget: a route that has not answered in this long is reported as hanging
ROUTE_TIMEOUT_S = 20


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def checked_service(node: Optional[dict], services: Optional[list]):
    """The ONE measured service whose API this step checks, or None.

    A check step (its title leads with verify/check/test/confirm…) that asks about an API, routes or endpoints, and
    names exactly one measured LXC service that has a directory and a port -- by its guest id in the title, or by its
    name in the step's words ("control panel" for `control-panel`)."""
    from app.modules.step_classify import _LEADING_OBSERVE_RE
    title = str((node or {}).get("title") or "")
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not _LEADING_OBSERVE_RE.match(title) or not _API_WORDS_RE.search(text):
        return None
    cands = [s for s in services or [] if getattr(s, "workdir", "") and getattr(s, "ports", ())
             and getattr(s, "guest", "") and not getattr(s, "vm", False)]
    m = re.search(r"\b(?:LXC|CT|container|guest)\s*(\d{3,5})\b", title, re.I)
    if m:
        hits = [s for s in cands if str(s.guest) == m.group(1)]
    else:
        low = f" {_norm(text)} "
        hits = [s for s in cands if _norm(getattr(s, "name", "")) and f" {_norm(s.name)} " in low]
    return hits[0] if len(hits) == 1 else None


def registered_routes(workspace: Optional[dict]) -> list[str]:
    """The GET /api routes the service's own code files register, in file order, once each."""
    out: list[str] = []
    for path, body in sorted((workspace or {}).items()):
        if "node_modules" in path or "scaffold-kit" in path or not path.endswith((".js", ".mjs", ".cjs", ".ts")):
            continue
        out += _ROUTE_RE.findall(str(body or ""))
    return list(dict.fromkeys(out))


def _sq(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def check_command(service, routes: list[str], login: Optional[tuple] = None) -> str:
    """One command, run inside the service's guest, that calls every route and reports each.

    `login` is `(secret name, user)` from the held logins (§17.1548): the password reaches the guest on stdin as the
    runner-resolved `<NAME>` placeholder, never in the command, the approval or a log."""
    port = str(next(iter(getattr(service, "ports", ()) or ()), ""))
    auth = f'-u "{login[1]}:$cpw" ' if login else ""
    body = (("cpw=$(cat); " if login else "")
            + "fail=0; for r in " + " ".join(routes) + "; do "
            + f"out=$(curl -sS -o /dev/null {auth}--max-time {ROUTE_TIMEOUT_S} -w \"%{{http_code}} %{{time_total}}s\" "
            + f"http://127.0.0.1:{port}$r 2>&1); code=${{out%% *}}; "
            + 'case "$code" in 2??) echo "PASS $out $r";; *) echo "FAIL $out $r"; fail=1;; esac; done; '
            + f'echo "{len(routes)} routes checked"; exit $fail')
    cmd = f"pct exec {service.guest} -- sh -c {_sq(body)}"
    return cmd + (f' <<< "<{login[0]}>"' if login else "")


def runbook(service, routes: list[str], login: Optional[tuple] = None) -> str:
    """The step's runbook: the one check, and a heading that says where its routes came from."""
    return (f"<!-- engine-capability: api_route_check -->\n\n"
            f"## Check every API route {getattr(service, 'name', 'the service')} registers\n\n"
            f"The {len(routes)} GET routes below are the ones the service's own code registers under "
            f"{getattr(service, 'workdir', '')}; each is called inside guest {service.guest} on port "
            f"{next(iter(service.ports), '')}" + (" with the login the engine holds" if login else "")
            + f", and one that does not answer 2xx within {ROUTE_TIMEOUT_S} s fails the step.\n\n"
            "## Run this\n\n```bash\n" + check_command(service, routes, login) + "\n```\n\n"
            # the run IS the check (it fails on any route); the read-only verify channel carries no secret, so its
            # check is the one that needs none: the service still active after every route was called
            "## Verify\n\n```bash\n" + f"pct exec {service.guest} -- systemctl is-active {service.unit}" + "\n```\n")


def login_for(service, logins: Optional[list[dict]]) -> Optional[tuple]:
    """`(secret name, user)` of the held login that belongs to this service, or None."""
    from app.modules.machine_values import _guarded_ports
    guard = _guarded_ports([service], logins or [])
    hit = next((guard[str(p)] for p in (getattr(service, "ports", ()) or ()) if str(p) in guard), None)
    return (hit[0], hit[1]) if hit else None
