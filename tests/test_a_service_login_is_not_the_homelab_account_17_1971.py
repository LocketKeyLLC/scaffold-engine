"""§17.1971 — a service's own login user is the one the engine holds for it, never the homelab account.

Live (job 7cc2a7ac T3v2 and T15, 2026-11-20): the frame asked for `PANEL_USER` and prefilled the operator's own
account — "the one account every pinned guest login names (PALWORLD_USER = …)" (§17.1606) — while the runner held
`PANEL_PASSWORD`, whose hint records the panel's Basic-auth user `panel`, and T3v2 had just used
`-u "panel:$PANEL_PASSWORD"` (http=200). A web app's Basic-auth user is not a Linux account on a guest.
Addresses below are made up.
"""
from __future__ import annotations

from app.modules import machine_values as mv
from app.modules import runbook_inputs as ri

# the live ledger's shape: one pinned guest login, CT 111 named control-panel in the map
ENV = {"profile": "root@pve",
       "substitutions": {"PALWORLD_USER": "operator1", "PALWORLD_IP": "10.20.30.44"},
       "system_state": {"host": {"kind": "host", "attrs": {"ip": "10.20.30.156"}},
                        "111": {"kind": "ct", "attrs": {"hostname": "control-panel"}}}}
HELD = [{"name": "PANEL_PASSWORD", "runner": "pve-runner", "for_operator": True,
         "hint": "the control panel's login password (user `panel`), operator decision 2026-10-08"}]
LOGINS = mv.logins_from_held(HELD)
T15 = ("Playwright headless: load each section, assert real human-readable data, no error text, no stuck 'Loading'. "
       "The panel is at http://10.20.30.25:3001.")
INPUT = {"name": "PANEL_USER", "hint": "the panel's Basic-auth user", "value": "", "secret": False}


def test_the_live_frame_fills_the_panels_own_user_and_says_where_it_came_from():
    out = ri.suggest_inputs([INPUT], ENV, text=T15, logins=LOGINS, held=["PANEL_PASSWORD"])[0]
    assert out["value"] == "panel"
    assert out["suggestions"][0] == {"value": "panel", "confidence": "held",
                                     "source": "the user of the login the engine holds for it (PANEL_PASSWORD)"}
    assert "operator1" not in [s["value"] for s in out["suggestions"]], "the homelab account is no candidate"


def test_a_held_password_with_no_recorded_user_leaves_it_unsuggested_not_guessed():
    out = ri.suggest_inputs([INPUT], ENV, text=T15, logins=[], held=["PANEL_PASSWORD"])[0]
    assert out["value"] == ""
    assert all(s["value"] != "operator1" for s in out["suggestions"])


def test_the_guest_login_still_comes_from_the_homelab_account():
    # §17.1606's case is untouched: AI_USER has no login of its own, so the guests' one account fills it
    ai = {"name": "AI_USER", "hint": "the account inside the guest", "value": "", "secret": False}
    out = ri.suggest_inputs([ai, INPUT], ENV, text=T15, logins=LOGINS, held=["PANEL_PASSWORD", "MASS_PASSWORD"])
    assert out[0]["value"] == "operator1" and out[0]["suggestions"][0]["confidence"] == "homelab"
    assert out[1]["value"] == "panel"


def test_the_operators_pin_by_name_still_wins():
    env = {**ENV, "substitutions": {**ENV["substitutions"], "PANEL_USER": "admin"}}
    out = ri.suggest_inputs([INPUT], env, text=T15, logins=LOGINS, held=["PANEL_PASSWORD"])[0]
    assert out["value"] == "admin"


def test_a_login_for_another_service_does_not_fill_this_one():
    assert ri.held_login_user("RADARR_USER", LOGINS) is None
    assert ri.held_login_user("PANEL_IP", LOGINS) is None
    assert not ri.service_login_name("RADARR_USER", LOGINS, ["PANEL_PASSWORD"])
    assert not ri.service_login_name("PANEL_USER", [], ["PANEL_API_TOKEN"]), "a token is not a login"


def test_frame_run_hands_the_held_logins_to_the_suggester():
    from app.modules import supervised_runs as sr
    node = {"node_key": "T15", "title": "Verify every panel section loads", "prompt_template": T15}
    rb = ("## Run this\n```bash\n"
          "curl -s -o /dev/null -w '%{http_code}\\n' -u \"<PANEL_USER>:$PANEL_PASSWORD\" http://10.20.30.25:3001/\n"
          "```\n")
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    policy = {"allow": ["ANY"], "sudo": True, "helper": "24", "held": ["PANEL_PASSWORD"], "held_logins": LOGINS,
              "secrets": [], "can_write_files": True}
    frame = sr.frame_run(node, rb, spec, policy, env=ENV)
    asked = {i["name"]: i for i in frame["inputs"]}
    assert asked["PANEL_USER"]["value"] == "panel", asked
