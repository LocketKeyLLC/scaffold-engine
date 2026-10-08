"""§17.1427 — a runner-held secret is offered from the engine's store too, and a literal value assigned to
it in a delivered file is refused.

Live, 2026-10-08, ADD127: the operator's PANEL_PASSWORD went to the engine's encrypted store (the policy's
`held` list); §17.1426 read only the runner's own `secrets` list, so the marker was never offered -- and
the model wrote `Environment=PANEL_PASSWORD=defrusciohomelab.duckdns.org` (the public domain, as the
password, in the clear). Read before approval; not approved.
"""
from __future__ import annotations

import inspect
import json
import pathlib

from app.modules import develop as dv
from app.modules import execution_agent

FIX = pathlib.Path(__file__).parent / "fixtures"
ADD127 = json.loads((FIX / "add127_node_2026_10_08.json").read_text())
CREDS = {"PANEL_PASSWORD": dv.RUNNER_HELD}
LIVE_UNIT = ("[Service]\nExecStart=/usr/bin/node server.js\nEnvironment=NODE_ENV=production\n"
             "Environment=PANEL_PASSWORD=defrusciohomelab.duckdns.org\n")


def test_the_live_unit_is_refused():
    refs = dv.a_secret_written_in_the_clear({"/etc/systemd/system/control-panel.service": LIVE_UNIT}, CREDS)
    assert len(refs) == 1 and "@@SCAFFOLD:PANEL_PASSWORD@@" in refs[0]["why"]


def test_the_marker_and_lookups_pass():
    ok = {"/etc/systemd/system/control-panel.service": "Environment=PANEL_PASSWORD=@@SCAFFOLD:PANEL_PASSWORD@@\n",
          "/opt/x/server.js": "if (password !== process.env.PANEL_PASSWORD) { deny(); }\nconst p = PANEL_PASSWORD;",
          "/opt/x/run.sh": 'export PANEL_PASSWORD="$PANEL_PASSWORD"\n',
          "/opt/x/auth.json": '{"PANEL_PASSWORD": "@@SCAFFOLD:PANEL_PASSWORD@@"}'}
    assert dv.a_secret_written_in_the_clear(ok, CREDS) == []


def test_a_json_literal_is_refused_too():
    assert dv.a_secret_written_in_the_clear({"/opt/x/auth.json": '{"PANEL_PASSWORD": "hunter2"}'}, CREDS)


def test_a_key_read_on_the_machine_is_not_this_rule():
    assert dv.a_secret_written_in_the_clear({"/x/c.json": '{"RADARR_API_KEY": "abc"}'}, {"RADARR_API_KEY": "pct exec …"}) == []


def test_the_engine_store_is_offered():
    assert dv.credentials_for([], ["PANEL_PASSWORD"], ADD127) == CREDS
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert '(policy or {}).get("held")' in src and "develop.a_secret_written_in_the_clear(files, creds)" in src
