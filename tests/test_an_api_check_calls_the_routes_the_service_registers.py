"""§17.1640 / §17.1641 — the control panel's API check (job 5db1b088 T26, 2026-10-18).

§17.1640: the engine's own `run_in_container` template writes the step's in-guest commands into a `REMOTE` heredoc
and sends that file in with `pct push "$GID" …`. `where_each_line_runs` read every heredoc line as the HOST's, so
§17.1368 refused `BASE = "http://127.0.0.1:3001"` -- a line that runs inside CT 111, where 3001 IS the panel.

§17.1641: the drafted check called five routes the panel does not have, with no login, on a panel that puts Basic
auth in front of all seventeen it does have. The engine reads the routes from the service's files and presents the
login it holds.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from app.modules import api_route_check as arc
from app.modules import machine_values as mv
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth, loopback_on_the_host, where_each_line_runs

PANEL = ServiceTruth(guest="111", unit="control-panel.service", name="control-panel", state="active",
                     ports=("3001",), address="192.168.1.25", workdir="/opt/control-panel-backend")
RADARR = ServiceTruth(guest="103", unit="radarr.service", name="radarr", state="active", ports=("7878",),
                      address="192.168.1.22", workdir="/var/lib/radarr")
T26 = {"node_key": "T26", "title": "Verify control panel API", "tool": "Shell", "description": "",
       "prompt_template": ("Test every /api route on control panel: Palworld, media, scaffold engine, network/host, "
                           "overview/health. Confirm each answers without hanging. Report pass/fail per route.")}
LOGINS = mv.logins_from_held([{"name": "PANEL_PASSWORD", "for_operator": True,
                               "hint": "the control panel's login password (user `panel`)"}])
#: the panel's files, reduced to their route lines (read live with grep on CT 111, 2026-10-18)
WORKSPACE = {
    "/opt/control-panel-backend/server.js": "app.use(basicAuth);\napp.get('/api/capabilities', (req, res) => {});",
    "/opt/control-panel-backend/palworld_route.js": "app.get('/api/palworld/status', async (req, res) => {});",
    "/opt/control-panel-backend/media_library_route.js": "app.get('/api/media-library', h);\napp.post('/api/x', h);",
    "/opt/control-panel-backend/scaffold_route.js": ("app.get('/api/scaffold/status', h);\n"
                                                    "app.get('/api/scaffold/jobs/:id', h);"),
    "/opt/control-panel-backend/node_modules/express/lib/router.js": "router.get('/api/internal', h);",
}


# ── §17.1640: a heredoc pushed into a guest runs in that guest ───────────────

def _t26_template_file() -> str:
    body = rt.render(rt.RUN_IN_CONTAINER, {"GID": "111", "STEP": "T26", "VERIFY_INSIDE": "true",
                                          "REMOTE_COMMANDS": ("cat > /tmp/verify_api.py <<'EOF'\n"
                                                              "BASE = \"http://127.0.0.1:3001\"\nEOF\n"
                                                              "python3 /tmp/verify_api.py")})
    return body.split("```bash\n", 1)[1].split("\n```", 1)[0]


def test_the_template_body_runs_in_its_guest_and_is_not_refused():
    text = _t26_template_file()
    lines = text.split("\n")
    where = where_each_line_runs(text)
    base = next(i for i, ln in enumerate(lines, 1) if ln.startswith("BASE ="))
    assert where.get(base) == "111", where.get(base)
    assert loopback_on_the_host([], [{"path": "/tmp/in_ct_111.sh", "content": text}], [PANEL]) == []
    # the template's own host lines are still the host's
    push = next(i for i, ln in enumerate(lines, 1) if "pct push" in ln)
    assert where.get(push) in (None, "")


def test_a_heredoc_the_host_runs_is_still_judged_as_the_hosts():
    host = "python3 - <<EOF\nurlopen('http://127.0.0.1:3001/api/health')\nEOF"
    assert loopback_on_the_host([host], [], [PANEL])
    kept = "cat > /tmp/x.py <<'EOF'\nurlopen('http://127.0.0.1:3001/api')\nEOF\npython3 /tmp/x.py"   # never pushed
    assert loopback_on_the_host([], [{"path": "/tmp/a.sh", "content": kept}], [PANEL])


def test_a_heredoc_fed_to_pct_exec_runs_in_that_guest():
    text = "pct exec 111 -- bash <<'IN'\ncurl -s http://127.0.0.1:3001/api/health\nIN"
    assert where_each_line_runs(text).get(2) == "111"


# ── §17.1641: which step is an API check, and of what ────────────────────────

def test_t26_checks_the_panel():
    assert arc.checked_service(T26, [PANEL, RADARR]) is PANEL


def test_other_steps_are_not_claimed():
    fix = {**T26, "title": "Fix control panel API", "prompt_template": "fix failing /api routes"}
    unnamed = {**T26, "title": "Verify the API", "prompt_template": "Test every /api route. Report pass/fail per route."}
    both = {**T26, "prompt_template": "Test every /api route of control panel and radarr."}
    for node in (fix, unnamed, both):
        assert arc.checked_service(node, [PANEL, RADARR]) is None, node
    by_id = {**T26, "title": "CT 103: verify the API", "prompt_template": "every route answers"}
    assert arc.checked_service(by_id, [PANEL, RADARR]) is RADARR


def test_the_routes_are_the_ones_its_code_registers():
    assert arc.registered_routes(WORKSPACE) == ["/api/media-library", "/api/palworld/status",
                                                "/api/scaffold/status", "/api/capabilities"]


# ── the frame the engine builds from it ──────────────────────────────────────

def _frame(runbook):
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": ["PANEL_PASSWORD"], "held_logins": LOGINS}
    return sr.frame_run(T26, runbook, spec, policy, services=[PANEL, RADARR],
                        inventory={"vms": {}, "cts": {"111": "running"}, "names": {"111": "control-panel"}})


def test_the_check_runs_with_the_held_login_and_nothing_to_ask():
    routes = arc.registered_routes(WORKSPACE)
    login = arc.login_for(PANEL, LOGINS)
    assert login == ("PANEL_PASSWORD", "panel")
    frame = _frame(arc.runbook(PANEL, routes, login))
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    (cmd,) = frame["commands"]
    assert cmd.startswith("pct exec 111 -- sh -c ") and '-u "panel:$cpw"' in cmd
    # the placeholder became the runner's reference: the value is injected on the runner, fed on stdin
    assert cmd.endswith('<<< "$PANEL_PASSWORD"') and "PANEL_PASSWORD" not in cmd.split("<<<")[0]
    assert all(r in cmd for r in routes)
    assert frame["verify"][0] == "pct exec 111 -- systemctl is-active control-panel.service"


def test_no_login_is_presented_to_a_service_it_does_not_belong_to():
    assert arc.login_for(RADARR, LOGINS) is None
    assert "-u " not in arc.check_command(RADARR, ["/api/v3/system/status"], None)


# ── bash level: every route is reported, and one failure fails the step ──────

def _run(tmp_path: Path, codes: dict) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"; bindir.mkdir(exist_ok=True)
    table = " ".join(f'*{r}) echo -n "{c} 0.012s";;' for r, c in codes.items())
    (bindir / "curl").write_text(f'#!/bin/bash\nfor a; do u="$a"; done\ncase "$u" in {table} '
                                 f'*) echo -n "000 20.000s"; exit 28;; esac\n')
    (bindir / "curl").chmod(0o755)
    (bindir / "pct").write_text('#!/bin/bash\nshift 3\nexec "$@"\n')    # `pct exec 111 -- sh -c …` -> `sh -c …`
    (bindir / "pct").chmod(0o755)
    cmd = arc.check_command(PANEL, list(codes) + ["/api/hangs"], ("PANEL_PASSWORD", "panel"))
    cmd = cmd.replace('<<< "<PANEL_PASSWORD>"', "<<< 'pw'")
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                          env={"PATH": f"{bindir}:/usr/bin:/bin"})


def test_every_route_is_reported_and_a_failure_fails_the_step(tmp_path):
    r = _run(tmp_path, {"/api/health": "200", "/api/overview": "401"})
    out = r.stdout.splitlines()
    assert "PASS 200 0.012s /api/health" in out and "FAIL 401 0.012s /api/overview" in out
    assert any(ln.startswith("FAIL 000") and ln.endswith("/api/hangs") for ln in out), out
    assert out[-1] == "3 routes checked" and r.returncode == 1, (r.stdout, r.stderr)


def test_all_routes_answering_passes(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    (bindir / "curl").write_text('#!/bin/bash\necho -n "200 0.010s"\n'); (bindir / "curl").chmod(0o755)
    (bindir / "pct").write_text('#!/bin/bash\nshift 3\nexec "$@"\n'); (bindir / "pct").chmod(0o755)
    cmd = arc.check_command(PANEL, ["/api/a", "/api/b"], None)
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env={"PATH": f"{bindir}:/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout.count("PASS") == 2, r.stdout


# ── the run IS the check: never "already met", never redrafted by the model ──

def test_the_engines_check_is_final_and_is_never_already_met():
    """Live (T26, the first frame this module built): §17.1302 read `systemctl is-active control-panel.service`,
    judged the goal met and recorded T26 done -- none of the 17 routes was called."""
    import inspect
    from app.modules import execution_agent
    assert arc.run_is_the_check(arc.runbook(PANEL, ["/api/a"], None))
    assert not arc.run_is_the_check("## Run this\n\n```bash\ncurl -s http://127.0.0.1:3001/api/a\n```\n")
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "_engine_check = _run_is_the_check(runbook)" in src
    for guard in ('_ro = "" if (_developed or frame.get("expects") or _engine_check) else',
                  '_missing = [] if (_developed or frame.get("expects") or _engine_check) else',
                  "_state_only = not supervised_runs.may_be_already_met(run_node, frame, _developed, _engine_check)"):
        assert guard in src, guard
    # §17.1692 — the guard moved into one function; it still says no for the engine's own check
    from app.modules import supervised_runs
    assert supervised_runs.may_be_already_met({"title": "Change the panel"}, {"verify": ["curl x"]}, engine_check=True) is False


# ── §17.1646: `'\''` reopens a single-quoted payload ─────────────────────────

def test_an_escaped_quote_keeps_the_payload_in_its_guest():
    text = ("pct exec 103 -- python3 -c 'import urllib.request\n"
            "u = urllib.request.urlopen('\\''http://127.0.0.1:7878/api/v3/system/status'\\'')\n"
            "print(u.status)' radarr\n"
            "curl -s http://127.0.0.1:7878/api")
    where = where_each_line_runs(text)
    assert where.get(2) == "103" and where.get(3) == "103", where
    assert where.get(4) in (None, ""), where                       # after the payload closes: the host again
    radarr = ServiceTruth(guest="103", unit="radarr.service", name="radarr", ports=("7878",))
    hits = loopback_on_the_host([text], [], [radarr])
    assert len(hits) == 1 and hits[0]["command"].startswith("curl -s http://127.0.0.1:7878"), hits


# ── §17.1651: the page's SECTIONS -- each route answers AND carries the fields the page reads ─────────────

T27 = {"node_key": "T27", "title": "Verify control panel sections", "tool": "Shell", "description": "",
       "prompt_template": "Check each section of control panel page shows real data rather than errors or 'Loading'. "
                          "Report pass/fail per section."}
PAGE = {"/opt/control-panel-ui/index.html": (
    "<script>async function loadPal(){ const res = await fetch(`${API_BASE}/api/palworld/status`);\n"
    "const data = await res.json(); el.textContent = data.status + ' ' + data.host; }\n"
    "async function loadMedia(){ const r = await fetch(`${API_BASE}/api/media-library`);\n"
    "const lib = await r.json(); show(lib.movies, lib.series); }</script>")}


def test_t27_is_a_sections_check_and_reads_what_each_section_reads():
    assert arc.checked_service(T27, [PANEL, RADARR]) is PANEL and arc.asks_about_sections(T27)
    assert not arc.asks_about_sections(T26)
    assert arc.section_reads(PAGE) == {"/api/palworld/status": ["status", "host"], "/api/media-library": ["movies", "series"]}


def test_the_sections_frame_runs_with_the_login_and_nothing_to_ask():
    frame = _frame(arc.sections_runbook(PANEL, arc.section_reads(PAGE), ("PANEL_PASSWORD", "panel")))
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert frame["commands"][0].endswith('<<< "$PANEL_PASSWORD"')


def test_a_section_missing_a_field_or_failing_fails_the_step(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    (bindir / "curl").write_text(
        # §17.1674 — the check reads the body from stdout with `-w " HTTPCODE%{http_code}"` (no temp file)
        '#!/bin/bash\nfor a; do u="$a"; done\ncase "$u" in\n'
        '*/api/palworld/status) printf \'{"status":"running","host":"192.168.1.44"} HTTPCODE200\';;\n'
        '*/api/media-library) printf \'{"movies":[]} HTTPCODE200\';;\n'
        '*) printf \'{"error":"Authentication required"} HTTPCODE401\';;\nesac\n')
    (bindir / "curl").chmod(0o755)
    (bindir / "pct").write_text('#!/bin/bash\nshift 3\nexec "$@"\n'); (bindir / "pct").chmod(0o755)
    reads = {**arc.section_reads(PAGE), "/api/overview": ["services"]}
    cmd = arc.sections_command(PANEL, reads, ("PANEL_PASSWORD", "panel")).replace('<<< "<PANEL_PASSWORD>"', "<<< 'pw'")
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env={"PATH": f"{bindir}:/usr/bin:/bin"})
    out = r.stdout.splitlines()
    assert "PASS /api/palworld/status (2 fields, nothing reported failing)" in out, r.stdout + r.stderr
    assert "FAIL /api/media-library HTTP 200, the page reads fields its answer lacks: series" in out, out
    assert "FAIL /api/overview HTTP 401" in out and out[-1] == "3 sections checked" and r.returncode == 1, out


def test_the_pause_reads_the_served_page_for_a_sections_check():
    import inspect
    from app.modules import execution_agent
    src = inspect.getsource(execution_agent._pause_for_decision)
    i = src.index("if _arc.asks_about_sections(run_node):")
    assert "develop.served_entries(_ws)" in src[i:i + 900] and "_arc.section_reads(_ws)" in src[i:i + 900]


def test_the_older_probe_never_overwrites_the_engines_check():
    """§17.1662 — a step worded for both (§17.1574c fires on "authenticated") keeps the engine's composed check."""
    node = {**T26, "prompt_template": T26["prompt_template"] + " Probe each endpoint authenticated as the panel user."}
    rb = arc.runbook(PANEL, ["/api/a", "/api/b"], ("PANEL_PASSWORD", "panel"))
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": ["PANEL_PASSWORD"], "held_logins": LOGINS}
    frame = sr.frame_run(node, rb, spec, policy, services=[PANEL, RADARR],
                         inventory={"vms": {}, "cts": {"111": "running"}, "names": {"111": "control-panel"}})
    assert "for r in /api/a /api/b" in frame["commands"][0], frame["commands"]
