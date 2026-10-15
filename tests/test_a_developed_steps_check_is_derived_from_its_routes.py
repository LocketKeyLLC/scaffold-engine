r"""§17.1582 — a fix step's feature check, read off the service's own routes when its text names none.

Live (c2792d4b T4 "Fix Radarr endpoint backend", 2026-10-15): developed on the panel (CT 111) after §17.1581, then
refused by §17.1418 — "its text must name the check in backticks" — while the engine held the service's code
(`read_workspace`) and the panel's probe one step upstream. The routes whose handler file carries a word of the
step's title are the step's feature; the check is a plain `curl --fail-with-body` of the loopback port.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace as NS

from app.modules import develop as dv

HOST = dv.Host(guest="111", workdir="/opt/control-panel-backend", unit="control-panel.service", vm=False)
PANEL = NS(name="control-panel", guest="111", unit="control-panel.service", ports=("3001",), workdir="/opt/control-panel-backend", vm=False)
WS = {
    "/opt/control-panel-backend/media_library_route.js": "const radarrUrl = cfg.radarrUrl;\nmodule.exports = (app) => {\n  app.get('/api/media-library', async (req, res) => { /* radarr + sonarr */ });\n};\n",
    "/opt/control-panel-backend/media_request_route.js": "// radarr request\nmodule.exports = (app) => { app.post('/api/media-request', async (req, res) => {}); };\n",
    "/opt/control-panel-backend/scaffold_route.js": "module.exports = (app) => {\n  app.get('/api/scaffold/health', async (req, res) => {});\n  app.get(\"/api/scaffold/jobs\", async (req, res) => {});\n};\n",
    "/opt/control-panel-backend/palworld_route.js": "module.exports = (app) => { app.get('/api/palworld/status', (req, res) => {}); app.post('/api/palworld/start', (req, res) => {}); };\n",
    "/opt/control-panel-backend/network_view_route.js": "module.exports = (app) => { app.get('/api/network-view', async (req, res) => {}); };\n",
    "/opt/control-panel-backend/server.js": "app.use(basicAuth);\napp.get('/api/capabilities', (req, res) => {});\n",
    "/opt/control-panel-ui/index.html": "<html><div id='overview'>Loading overview...</div></html>",
    "/opt/control-panel-backend/node_modules/express/lib/router/index.js": "app.get('/x', fn)",
}
T4 = {"node_key": "T4", "title": "Fix Radarr endpoint backend", "tool": "Shell", "description": "",
      "prompt_template": "Edit the Radarr handler on container 111 to read RADARR_API_KEY from engine-held secrets, send proper X-Api-Key header, and return JSON data or a clear JSON error."}
T7 = {"node_key": "T7", "title": "Fix remaining endpoint backends", "tool": "Shell",
      "prompt_template": "For any endpoint not covered by Radarr/Sonarr/Scaffold (e.g., Palworld, media, network, FlareSolverr), fix its handler on container 111 to return real data or a clear JSON error."}
T6 = {"node_key": "T6", "title": "Fix Scaffold endpoint backend", "tool": "Shell", "prompt_template": "Edit the Scaffold handler on container 111 …"}
T8 = {"node_key": "T8", "title": "Fix UI fetch error handling", "tool": "Shell",
      "prompt_template": "Edit the hand-written UI on container 111 so every section's fetch has a timeout and a .catch that replaces 'Loading ...' with the error message."}
BASE = "pct exec 111 -- curl -s --fail-with-body --max-time 20 http://127.0.0.1:3001"


def test_the_routes_whose_handler_names_the_steps_feature_are_its_checks():
    assert dv.service_port(HOST, [PANEL]) == "3001" and dv.service_port(HOST, []) == ""
    assert dv.derived_checks(T4, HOST, WS, "3001") == [f"{BASE}/api/media-library"]        # radarr: the GET route, not the POST
    assert dv.derived_checks(T6, HOST, WS, "3001") == [f"{BASE}/api/scaffold/health", f"{BASE}/api/scaffold/jobs"]
    t7 = dv.derived_checks(T7, HOST, WS, "3001")
    assert f"{BASE}/api/palworld/status" in t7 and f"{BASE}/api/network-view" in t7 and f"{BASE}/api/media-library" in t7
    assert not any("capabilities" in c or "node_modules" in c or "/x" in c for c in t7)
    assert dv.derived_checks(T8, HOST, WS, "3001") == [f"{BASE}/"]                           # a UI step: the served page
    assert dv.derived_checks(T4, HOST, WS, "") == [] and dv.derived_checks(T4, HOST, {}, "3001") == []
    nothing = {"title": "Fix the thing", "tool": "Shell", "prompt_template": "on container 111"}
    assert dv.derived_checks(nothing, HOST, WS, "3001") == []
    from app.modules.assist_state_check import read_only_command
    assert all(read_only_command(c) for c in t7)


def test_a_derived_check_lifts_the_no_own_check_refusal_and_rides_the_done_checks():
    assert dv.own_check_refusal(T4, HOST)                                          # the text names none: refused, as before
    derived = dv.derived_checks(T4, HOST, WS, "3001")
    assert dv.own_check_refusal(T4, HOST, derived) == []
    checks = dv.done_checks(T4, HOST, derived)
    assert checks[0] == f"{BASE}/api/media-library" and checks[-1] == "pct exec 111 -- systemctl is-active control-panel.service"
    assert dv.done_checks(T4, HOST) == ["pct exec 111 -- systemctl is-active control-panel.service"]
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "_derived = develop.derived_checks(run_node, host, workspace, develop.service_port(host, _services))" in src
    assert src.index("workspace = await develop.read_workspace(spec, host)") < src.index("develop.own_check_refusal(run_node, host, _derived)")
    assert "checks = develop.done_checks(run_node, host, _derived)" in src
