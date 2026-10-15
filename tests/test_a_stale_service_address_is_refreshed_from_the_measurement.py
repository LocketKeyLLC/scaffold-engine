r"""§17.1590 — the proxies are services the engine names and measures; a stale address copy is refused or refreshed.

Live (c2792d4b T7 "Fix remaining endpoint backends … (e.g., Palworld, media, network, FlareSolverr)"): `_NAME_RE` knew
neither FlareSolverr nor Byparr and `names_in` read title+description while the planner wrote the body into
prompt_template, so neither was measured; the model kept config/byparr.json's `192.168.1.25` and the delivery's own
check answered `connect ECONNREFUSED 192.168.1.25:8192` while Byparr sits on .27. And T8 "Fix UI fetch error handling"
names no service, so `service_port` found none and the UI's page check could not be derived.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace as NS

from app.modules import develop as dv
from app.modules import service_truth as st

BYPARR = NS(name="byparr", guest="108", unit="byparr.service", ports=("8192",), address="192.168.1.27", workdir="", vm=False)
FLARE = NS(name="flaresolverr", guest="107", unit="flaresolverr.service", ports=("8191",), address="192.168.1.26", workdir="", vm=False)
PANEL = NS(name="control-panel", guest="111", unit="control-panel.service", ports=("3001",), address="192.168.1.24", workdir="/opt/control-panel-backend", vm=False)
HOST = dv.Host(guest="111", workdir="/opt/control-panel-backend", unit="control-panel.service", vm=False)
T7 = {"title": "Fix remaining endpoint backends", "description": "",
      "prompt_template": "For any endpoint not covered by Radarr/Sonarr/Scaffold (e.g., Palworld, media, network, FlareSolverr), fix its handler on container 111."}


def test_the_proxies_are_services_the_step_names_wherever_its_words_are():
    assert "flaresolverr" in st.names_in(T7) and "palworld" in st.names_in(T7) and "radarr" in st.names_in(T7)
    assert st.names_in({"title": "Fix the Byparr health route", "description": ""}) == ["byparr"]
    assert st.names_in({"title": "Grow the disk", "description": "nothing here"}) == []


def test_a_develop_host_carries_its_port_for_a_step_that_names_no_service():
    h = dv.host_for({"title": "Fix UI fetch error handling", "tool": "Shell",
                     "prompt_template": "Edit the hand-written UI on container 111 so every section's fetch has a timeout and a .catch."},
                    [PANEL], brief="Fix the UI on container 111")
    assert h is not None and h.port == "3001" and dv.service_port(h, []) == "3001" and dv.service_port(h, [PANEL]) == "3001"
    assert dv.service_port(HOST, []) == ""                                                       # nothing measured, nothing remembered


def test_a_stale_address_for_a_measured_service_is_refused():
    files = {"/opt/control-panel-backend/byparr_route.js": "const r = await http('http://192.168.1.25:8192/health');\n",
             "/opt/control-panel-backend/config/byparr.json": '{\n  "host": "192.168.1.25",\n  "port": 8192\n}\n'}
    out = dv.stale_service_addresses(files, [BYPARR, FLARE, PANEL])
    assert len(out) == 2 and all("192.168.1.27" in o["why"] and "byparr" in o["why"] for o in out)
    assert dv.stale_service_addresses({"/x/byparr.json": '{"host": "192.168.1.27", "port": 8192}'}, [BYPARR]) == []     # the measured address
    assert dv.stale_service_addresses({"/x/other.js": "fetch('http://192.168.1.25:8192/')"}, [NS(name="byparr", address="", ports=("8192",), guest="108")]) == []   # unmeasured: no judgment
    assert dv.stale_service_addresses({"/x/notes.md": "the old host was 192.168.1.25"}, [BYPARR]) == []                  # a file named after no service, no port


def test_a_service_named_config_is_delivered_with_the_measured_address():
    ws = {"/opt/control-panel-backend/config/byparr.json": '{\n  "host": "192.168.1.25",\n  "port": 8192\n}\n',
          "/opt/control-panel-backend/config/flaresolverr.json": '{\n  "host": "192.168.1.26",\n  "port": 8191\n}\n',
          "/opt/control-panel-backend/byparr_route.js": "// code; not a config named after the service? it is: byparr_route.js\nconst h = '192.168.1.25';\n"}
    files = {"/opt/control-panel-backend/byparr_route.js": "// the model's own version\n"}
    out, added = dv.refresh_address_copies(files, ws, [BYPARR, FLARE, PANEL], HOST, {"title": "t"})
    assert added == ["/opt/control-panel-backend/config/byparr.json"]                        # flaresolverr.json already right; the route is the model's
    assert out["/opt/control-panel-backend/config/byparr.json"] == '{\n  "host": "192.168.1.27",\n  "port": 8192\n}\n'
    assert out["/opt/control-panel-backend/byparr_route.js"] == "// the model's own version\n"
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "files, _readdr = develop.refresh_address_copies(files, workspace, _services, host, run_node)" in src
    assert "_dead = (develop.stale_service_addresses(files, _services)" in src
    assert src.index("refresh_credential_copies(files, workspace") < src.index("refresh_address_copies(files, workspace") < src.index("derived_from_files(host, files, _port)")
