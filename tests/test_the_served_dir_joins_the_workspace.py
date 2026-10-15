r"""§17.1592 — the served UI directory joins the develop workspace; a proxy the files name is a service in play.

Live (c2792d4b T8 "Fix UI fetch error handling"): `develop_no_derived_check port='3001' files=27 html=0` — the panel
serves /opt/control-panel-ui (a sibling of its WorkingDirectory), the brief named no sibling dir, so the page the step is
about was never read. And T7's text said FlareSolverr and never Byparr, so byparr.json kept 192.168.1.25:8192.
"""
from __future__ import annotations

import inspect

from app.modules import develop as dv

HOST = dv.Host(guest="111", workdir="/opt/control-panel-backend", unit="control-panel.service", vm=False, port="3001")
WS = {"/opt/control-panel-backend/server.js": "const path = require('path');\napp.use(express.static(path.join(__dirname, '..', 'control-panel-ui')));\n",
      "/opt/control-panel-backend/config/byparr.json": '{"host": "192.168.1.25", "port": 8192}',
      "/opt/control-panel-backend/byparr_route.js": "// byparr"}


def test_the_served_directory_is_read_and_writable_for_the_step():
    assert dv.served_dirs(WS) == ["/opt/control-panel-ui"]
    host2, added = dv.widen_to_served(HOST, WS)
    assert added == ["/opt/control-panel-ui"] and host2.extra_dirs == ("/opt/control-panel-ui",) and host2.port == "3001"
    assert dv.allowed_path("/opt/control-panel-ui/index.html", host2, {"title": "t"}) and not dv.allowed_path("/opt/control-panel-ui/index.html", HOST, {"title": "t"})
    assert dv.widen_to_served(host2, WS) == (host2, [])                                         # idempotent
    inside = {"/opt/control-panel-backend/server.js": "app.use(express.static(path.join(__dirname, 'public')));\n"}
    assert dv.widen_to_served(HOST, inside) == (HOST, [])                                        # under the workdir already
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "host, _widened = develop.widen_to_served(host, workspace)" in src and src.index("widen_to_served(host, workspace)") < src.index("develop.service_port(host, _services)")


def test_a_proxy_the_files_name_is_a_service_in_play():
    assert dv.proxies_in_workspace(WS) == ["byparr"] and dv.proxies_in_workspace({"/x/server.js": ""}) == []
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "_ws_proxies = supervised_runs.engine_deployed_services(develop.proxies_in_workspace(workspace), _services)" in src
    assert "_services.extend(_ws_proxies)" in src and src.index("_services.extend(_ws_proxies)") < src.index("facts = _st_dev.table(_services, _reading)")
