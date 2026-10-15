r"""§17.1591 — the engine's own deploys are services a step may name; a service's own config gets its port too.

Live (c2792d4b T7): FlareSolverr and Byparr run as Docker containers inside CT 107/108, guests the unit-based
measurement cannot see into; nothing was measured, so the model wrote `192.168.1.107` for FlareSolverr and kept
`192.168.1.25:8192` for Byparr. The engine put them there itself: 192.168.1.26:8191 (§17.1516) and 192.168.1.27:8191
(§17.1535). Byparr's config also carried the wrong PORT (8192), which an address refresh alone would have kept.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace as NS

from app.modules import develop as dv
from app.modules import supervised_runs as sr

HOST = dv.Host(guest="111", workdir="/opt/control-panel-backend", unit="control-panel.service", vm=False)


def test_the_engines_own_deploys_answer_for_the_proxies_a_step_names():
    own = sr.engine_deployed_services(["palworld", "flaresolverr", "byparr"], [])
    assert [(s.name, s.guest, s.address, s.ports) for s in own] == [("flaresolverr", "107", "192.168.1.26", ("8191",)), ("byparr", "108", "192.168.1.27", ("8191",))]
    assert sr.engine_deployed_services(["byparr"], [NS(name="byparr", guest="108")]) == []          # measured for real: not doubled
    assert sr.engine_deployed_services(["radarr"], []) == []
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "_own = supervised_runs.engine_deployed_services(_named, _services)" in src and "_services += _own" in src


def test_a_services_own_config_is_refreshed_and_gated_on_its_port_as_well_as_its_address():
    byparr = sr.engine_deployed_services(["byparr"], [])[0]
    ws = {"/opt/control-panel-backend/config/byparr.json": '{\n  "host": "192.168.1.25",\n  "port": 8192\n}\n'}
    out, added = dv.refresh_address_copies({}, ws, [byparr], HOST, {"title": "t"})
    assert added == ["/opt/control-panel-backend/config/byparr.json"]
    assert out["/opt/control-panel-backend/config/byparr.json"] == '{\n  "host": "192.168.1.27",\n  "port": 8191\n}\n'
    right = {"/opt/control-panel-backend/config/byparr.json": '{\n  "host": "192.168.1.27",\n  "port": 8192\n}\n'}   # address right, port wrong
    out2, added2 = dv.refresh_address_copies({}, right, [byparr], HOST, {"title": "t"})
    assert added2 and '"port": 8191' in out2["/opt/control-panel-backend/config/byparr.json"]
    gate = dv.stale_service_addresses(right, [byparr])
    assert len(gate) == 1 and "port 8192" in gate[0]["command"] and "8191" in gate[0]["why"]
    assert dv.stale_service_addresses({"/x/byparr.json": '{"host": "192.168.1.27", "port": 8191}'}, [byparr]) == []
    assert dv.refresh_address_copies({}, {"/x/byparr.json": '{"host": "192.168.1.27", "port": 8191}'}, [byparr], HOST, {"title": "t"})[1] == []
