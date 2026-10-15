r"""§17.1583 — three seams of one develop frame (c2792d4b T4, 2026-10-15).

(a) §17.1548's held-login check wrapped the derived curl in `IFS= read -r cpw` — the read-only verify channel refuses
    `read` (§17.1390), so every round was refused for the ENGINE'S OWN check; (b) eight identical rounds followed, the
    model never able to change it; (c) the model wrote the panel's config back whole with Sonarr's API key in the
    clear, because `read_workspace` had handed it the key.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace as NS

from app.modules import develop as dv
from app.modules import machine_values as mv
from app.modules import supervised_runs as sr

K = "55a5fd4184064126b9ba14dd1820bd65"
WS = {"/opt/control-panel-backend/config/media-request.json": '{\n  "radarrApiKey": "@@SCAFFOLD:RADARR_API_KEY@@",\n  "sonarrApiKey": "' + K + '"\n}\n',
      "/opt/control-panel-backend/.env": f"SONARR_API_KEY={K}\nNODE_ENV=production\n",
      "/opt/control-panel-backend/server.js": 'const ServerId = "df37d1e5fde44e2388f31af6f88806d9";\nconst sonarrApiKey = cfg.sonarrApiKey;\n'}
CREDS = {"SONARR_API_KEY": "pct exec 104 -- grep -oP '(?<=<ApiKey>)[^<]+' /var/lib/sonarr/config.xml", "RADARR_API_KEY": dv.RUNNER_HELD}


def test_a_credential_the_workspace_carries_reaches_the_model_as_its_mark():
    ws, marked = dv.mark_held_credentials(WS, CREDS)
    assert marked == ["SONARR_API_KEY"]
    assert K not in "\n".join(ws.values())
    assert '"sonarrApiKey": "@@SCAFFOLD:SONARR_API_KEY@@"' in ws["/opt/control-panel-backend/config/media-request.json"]
    assert '"radarrApiKey": "@@SCAFFOLD:RADARR_API_KEY@@"' in ws["/opt/control-panel-backend/config/media-request.json"]   # already a mark
    assert "SONARR_API_KEY=@@SCAFFOLD:SONARR_API_KEY@@" in ws["/opt/control-panel-backend/.env"]
    assert ws["/opt/control-panel-backend/server.js"] == WS["/opt/control-panel-backend/server.js"]                         # a GUID, a lookup: untouched
    # a key whose name is no credential the engine holds is left alone (and §17.1391 refuses it in a proposed file)
    other = {"/x/cfg.json": '{"plexToken": "' + K + '"}'}
    assert dv.mark_held_credentials(other, CREDS) == (other, [])
    assert dv.mark_held_credentials(WS, {}) == (WS, [])
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "workspace, _marked = develop.mark_held_credentials(workspace, creds)" in src
    assert src.index("creds = develop.credentials_for(") < src.index("mark_held_credentials(workspace, creds)") < src.index("develop.build_prompt(")


def test_a_json_style_key_is_a_credential_shape_too():
    text = '{"sonarrApiKey": "' + K + '"}'
    assert K not in sr.mask_credential_literals(text) and "sonarrApiKey" in sr.mask_credential_literals(text)
    assert len(sr.a_credential_written_as_a_literal([], None, [{"path": "/x/c.json", "content": text}])) == 1
    for ok in ('{"sonarrApiKey": "@@SCAFFOLD:SONARR_API_KEY@@"}', '{"sonarrApiKey": "<SONARR_API_KEY>"}', 'ServerId":"df37d1e5fde44e2388f31af6f88806d9"'):
        assert sr.mask_credential_literals(ok) == ok, ok


def test_the_held_login_check_is_a_shape_the_verify_channel_takes():
    svc = NS(name="control-panel", guest="111", unit="control-panel.service", ports=("3001",), workdir="/opt/control-panel-backend", vm=False)
    logins = [{"name": "PANEL_PASSWORD", "user": "panel", "hint": "the control panel's login password (user `panel`)"}]
    naked = "pct exec 111 -- curl -s --fail-with-body --max-time 20 http://127.0.0.1:3001/api/media-library"
    _, verify, notes = mv.present_a_held_login([], [naked], [svc], logins)
    assert notes and "cpw=$(cat); " in verify[0] and "read -r" not in verify[0]
    assert sr.a_check_the_channel_will_refuse(verify) == []          # live: "the verify channel will refuse this check", 8 rounds


def test_the_develop_loop_stops_when_a_round_brings_nothing_new():
    assert dv.stalled("- the verify channel will refuse this check", "- the verify  channel will refuse this check")
    assert not dv.stalled("- a", "- b") and not dv.stalled("", "") and not dv.stalled("- a", "")
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "if develop.stalled(evidence, _this_ev):" in src and src.index("develop.stalled(evidence, _this_ev)") < src.index("current, evidence = best[2], develop.evidence_of(best[1])")
