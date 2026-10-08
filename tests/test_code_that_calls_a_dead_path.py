"""§17.1422 — a developed version that calls a path the engine MEASURED as not answering is refused.

Live, 2026-10-08, ADD125: the develop facts said `/api/stats/summary` → HTTP 200 and `/admin/api.php?summary`
→ HTTP 400 (Pi-hole v6.4.3, read just now), and the model still wrote the v5 path -- the step text asked for
an "API token", a v5 notion. The sandbox reaches no machine, so the route answering at all passed it.
"""
from __future__ import annotations

import inspect

from app.modules import develop as dv
from app.modules import execution_agent

STATUS = [{"service": "pihole", "path": "/api/stats/summary", "code": "200", "address": "192.168.1.30"},
          {"service": "pihole", "path": "/admin/api.php?summary", "code": "400", "address": "192.168.1.30"}]
V5 = {"/opt/control-panel-backend/network_view_route.js":
      "const summary = await httpRequest(piholeAddress, 80, `/admin/api.php?summary&auth=${apiToken}`, 'GET');"}
V6 = {"/opt/control-panel-backend/network_view_route.js":
      "const summary = await httpRequest(host, 80, '/api/stats/summary');"}


def test_the_live_v5_call_is_refused_and_told_what_answers():
    refs = dv.calls_a_dead_path(V5, STATUS)
    assert len(refs) == 1
    why = refs[0]["why"]
    assert "`/admin/api.php` on pihole (192.168.1.30)" in why and "HTTP 400" in why
    assert "`/api/stats/summary` (HTTP 2xx, no key)" in why


def test_the_v6_call_passes():
    assert dv.calls_a_dead_path(V6, STATUS) == []


def test_an_unread_path_judges_nothing():
    assert dv.calls_a_dead_path(V5, [{**STATUS[1], "code": ""}]) == []


def test_the_loop_reads_once_and_judges_every_round():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "status = await develop.read_status(spec, _services)" in src
    assert "develop.calls_a_dead_path(files, status)" in src
