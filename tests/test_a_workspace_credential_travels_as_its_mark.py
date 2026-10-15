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
    ws, marked, creds = dv.mark_held_credentials(WS, CREDS)
    assert marked == ["SONARR_API_KEY"] and creds == CREDS
    assert K not in "\n".join(ws.values())
    assert '"sonarrApiKey": "@@SCAFFOLD:SONARR_API_KEY@@"' in ws["/opt/control-panel-backend/config/media-request.json"]
    assert '"radarrApiKey": "@@SCAFFOLD:RADARR_API_KEY@@"' in ws["/opt/control-panel-backend/config/media-request.json"]   # already a mark
    assert "SONARR_API_KEY=@@SCAFFOLD:SONARR_API_KEY@@" in ws["/opt/control-panel-backend/.env"]
    assert ws["/opt/control-panel-backend/server.js"] == WS["/opt/control-panel-backend/server.js"]                         # a GUID, a lookup: untouched
    # a key whose name is no credential the engine holds is left alone (and §17.1391 refuses it in a proposed file)
    other = {"/x/cfg.json": '{"plexToken": "' + K + '"}'}
    assert dv.mark_held_credentials(other, CREDS) == (other, [], CREDS)
    assert dv.mark_held_credentials(WS, {}) == (WS, [], {})
    # §17.1585 — a HELD name the step's creds never mentioned: the unit file T4 delivered, read back for T5
    unit = {"/etc/systemd/system/control-panel.service": "[Service]\nEnvironment=NODE_ENV=production\nEnvironment=RADARR_API_KEY=971c0d2e4f6a8b1c3d5e7f9a0b2c4d6e\n"}
    ws2, marked2, creds2 = dv.mark_held_credentials(unit, {"SONARR_API_KEY": "read"}, ["RADARR_API_KEY", "PANEL_PASSWORD"])
    assert marked2 == ["RADARR_API_KEY"] and "Environment=RADARR_API_KEY=@@SCAFFOLD:RADARR_API_KEY@@" in ws2["/etc/systemd/system/control-panel.service"]
    assert creds2 == {"SONARR_API_KEY": "read", "RADARR_API_KEY": dv.RUNNER_HELD}                  # the delivery can fill it
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "workspace, _marked, creds = develop.mark_held_credentials(" in src and '(policy or {}).get("held") or []))' in src
    assert src.index("creds = develop.credentials_for(") < src.index("develop.mark_held_credentials(") < src.index("develop.build_prompt(")


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


def test_a_value_the_machine_holds_wins_over_the_runners_copy():
    """§17.1585 — live: the runner's RADARR_API_KEY was stale; Radarr's config.xml on its guest is the truth."""
    radarr = NS(name="radarr", guest="103", unit="radarr.service", ports=("7878",), workdir="/var/lib/radarr", vm=False)
    node = {"title": "Fix Radarr endpoint backend", "prompt_template": "read RADARR_API_KEY from engine-held secrets and send X-Api-Key"}
    creds = dv.credentials_for([radarr], ["RADARR_API_KEY", "PANEL_PASSWORD"], node)
    assert creds["RADARR_API_KEY"] != dv.RUNNER_HELD and "103" in creds["RADARR_API_KEY"] and "config.xml" in creds["RADARR_API_KEY"]
    assert "PANEL_PASSWORD" not in creds                                            # not named by the step
    assert dv.credentials_for([], ["RADARR_API_KEY"], node) == {"RADARR_API_KEY": dv.RUNNER_HELD}   # no machine read: the runner's copy


# ── §17.1587 — the engine locates the machine read itself, and delivers every file that holds a copy ─────────────
def test_a_credential_the_step_never_named_still_gets_the_machines_read_through_the_inventory():
    inv = {"cts": {"103": "running", "104": "running"}, "vms": {}, "names": {"103": "radarr", "104": "sonarr"}, "unread": []}
    r = dv.machine_read_for("RADARR_API_KEY", [], inv)
    assert r and "103" in r and "config.xml" in r
    assert dv.machine_read_for("RADARR_API_KEY", [], None) is None and dv.machine_read_for("PANEL_PASSWORD", [], inv) is None
    node = {"title": "Fix Radarr endpoint backend", "prompt_template": "read RADARR_API_KEY from engine-held secrets"}
    assert dv.credentials_for([], ["RADARR_API_KEY"], node, inventory=inv)["RADARR_API_KEY"] == r      # not the runner's stale copy
    assert dv.credentials_for([], ["RADARR_API_KEY"], node) == {"RADARR_API_KEY": dv.RUNNER_HELD}       # nothing to place it: the copy


def test_a_delivery_refreshes_every_file_that_holds_a_copy_of_a_held_credential():
    host = dv.Host(guest="111", workdir="/opt/control-panel-backend", unit="control-panel.service", vm=False)
    marked_ws = {"/opt/control-panel-backend/config/media-request.json": '{"radarrApiKey": "@@SCAFFOLD:RADARR_API_KEY@@"}',
                 "/etc/systemd/system/control-panel.service": "[Service]\nEnvironment=RADARR_API_KEY=@@SCAFFOLD:RADARR_API_KEY@@\n",
                 "/etc/other/thing.conf": "key=@@SCAFFOLD:RADARR_API_KEY@@"}
    files = {"/opt/control-panel-backend/media_library_route.js": "// model's", "/opt/control-panel-backend/config/media-request.json": "{\"model\": 1}"}
    out, added = dv.refresh_credential_copies(files, marked_ws, sorted(marked_ws), host, {"title": "t"})
    assert added == ["/etc/systemd/system/control-panel.service"]                      # the unit: refreshed; the config: the model's own version stands
    assert out["/opt/control-panel-backend/config/media-request.json"] == "{\"model\": 1}" and "/etc/other/thing.conf" not in out
    assert out["/etc/systemd/system/control-panel.service"].endswith("@@SCAFFOLD:RADARR_API_KEY@@\n")
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "files, _refreshed = develop.refresh_credential_copies(files, workspace, _marked_paths, host, run_node)" in src
    assert src.index("_marked_paths = sorted(") < src.index("refresh_credential_copies(files, workspace") < src.index("develop.derived_from_files(host, files, _port)")
    assert 'creds[_n] = develop.machine_read_for(_n, _services, _inv) or develop.RUNNER_HELD' in src
    assert "run_node, inventory=_inv)" in src
