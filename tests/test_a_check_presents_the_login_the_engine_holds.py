"""§17.1548 — a check presents the login the engine holds.

Live, control-panel job ae721781, 2026-10-14. The dark panel was built, correct
and serving on CT 111 :3001, but every verify curled it UNAUTHENTICATED:

    pct exec 111 -- curl -s --fail-with-body http://127.0.0.1:3001/api/health
    -> {"error":"Authentication required"}        # 401

The panel's own `server.js` applies `app.use(basicAuth)` to EVERY route (user
`panel`, password in the runner-held secret PANEL_PASSWORD). §17.1547 made the
check FAIL honestly on that 401 — right, but the engine was blind to the app it
had itself secured. The *arr family already presents a key it READS off the
machine; HTTP Basic auth from a STORED secret is a different kind, so it gets its
own presenter. The value never enters the command: the password is fed to the
guest on STDIN as a herestring, written as the `<NAME>` placeholder the runner
resolves — so it never reaches the host argv, the approval, or a sudo log.
"""
from __future__ import annotations

import re

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth

PANEL = ServiceTruth(guest="111", unit="control-panel.service", name="control-panel", ports=("3001",))
RADARR = ServiceTruth(guest="103", unit="radarr.service", name="radarr", ports=("7878",))

# exactly what the drafter wrote for the failing verify T22v2.
NAKED = "pct exec 111 -- curl -s --fail-with-body http://127.0.0.1:3001/api/health"

PANEL_LOGIN = [{"name": "PANEL_PASSWORD", "runner": "pve-runner", "for_operator": True,
                "hint": "the control panel's login password (user `panel`), operator decision 2026-10-08"}]


def _logins():
    return mv.logins_from_held(PANEL_LOGIN)


# ── logins_from_held: only an operator LOGIN becomes Basic auth ──────────────

def test_a_password_login_with_a_user_is_recognised():
    out = mv.logins_from_held(PANEL_LOGIN)
    assert out == [{"name": "PANEL_PASSWORD", "user": "panel",
                    "hint": PANEL_LOGIN[0]["hint"]}]


def test_an_api_key_is_not_a_login():
    # not for_operator, no …PASSWORD name, no user in the hint — each excludes it.
    meta = [
        {"name": "RADARR_API_KEY", "for_operator": False, "hint": "read from radarr's config.xml"},
        {"name": "PROWLARR_API_KEY", "for_operator": True, "hint": "an API key"},         # no PASSWORD
        {"name": "DB_PASSWORD", "for_operator": True, "hint": "the postgres password"},   # no user named
    ]
    assert mv.logins_from_held(meta) == []


# ── present_a_held_login: the HIT ────────────────────────────────────────────

def test_the_naked_panel_curl_gets_basic_auth():
    cmds, verify, notes = mv.present_a_held_login([NAKED], [], [PANEL], _logins())
    got = cmds[0]
    # wrapped to run in-guest through sh -c, password read from stdin into CPW,
    # presented to curl as `-u panel:$CPW`, fed by the herestring placeholder.
    assert got.startswith("pct exec 111 -- sh -c ")
    assert "IFS= read -r CPW" in got
    assert '-u "panel:$CPW"' in got
    assert got.rstrip().endswith('<<< "<PANEL_PASSWORD>"')
    # the original endpoint + flags survive.
    assert "--fail-with-body" in got and "http://127.0.0.1:3001/api/health" in got
    assert notes and "Basic auth" in notes[0]["why"]


def test_the_check_is_presented_too():
    _, verify, _ = mv.present_a_held_login([], [NAKED], [PANEL], _logins())
    assert '-u "panel:$CPW"' in verify[0]


# ── the SECRET never lands where a log would see it ──────────────────────────

def test_the_password_is_a_stdin_placeholder_never_in_the_argv():
    cmds, _, _ = mv.present_a_held_login([NAKED], [], [PANEL], _logins())
    got = cmds[0]
    # the ONLY reference to the secret is the herestring placeholder; the plaintext
    # is nowhere (it is not even available here), and `$CPW` — the thing in the
    # curl argv — is the guest's own stdin read, inside the single-quoted payload.
    assert got.count("<PANEL_PASSWORD>") == 1
    head = got.split("<<<")[0]
    assert "PANEL_PASSWORD" not in head          # the placeholder is only in the herestring tail
    assert "$CPW" in head                         # the argv carries the stdin var, not a value


def test_the_runner_resolves_the_placeholder_and_it_never_travels():
    # the whole chain: present -> inputs_for -> apply_runner_secrets, with the
    # engine holding PANEL_PASSWORD. The `<PANEL_PASSWORD>` becomes `$PANEL_PASSWORD`
    # for the runner to inject, and the name is reported resolved (nothing to ask).
    cmds, _, _ = mv.present_a_held_login([NAKED], [], [PANEL], _logins())
    inputs = sr.inputs_for(cmds, [], "", [])
    out_cmds, _, kept, resolved, missing = sr.apply_runner_secrets(
        cmds, [], inputs, {"secrets": [], "held": ["PANEL_PASSWORD"]})
    assert '<<< "$PANEL_PASSWORD"' in out_cmds[0]
    assert "<PANEL_PASSWORD>" not in out_cmds[0]
    assert "PANEL_PASSWORD" in resolved and missing == []
    # nothing left for the operator to type for it.
    assert not any(i.get("name") == "PANEL_PASSWORD" for i in kept)


# ── the correct MISSES (no login is presented where it does not belong) ──────

def test_a_curl_to_another_service_is_untouched():
    # a radarr curl on :7878 — no login names radarr, so it is left exactly as drafted.
    radarr = "pct exec 103 -- curl -s http://127.0.0.1:7878/api/v3/system/status"
    cmds, _, notes = mv.present_a_held_login([radarr], [], [PANEL, RADARR], _logins())
    assert cmds == [radarr] and notes == []


def test_a_curl_that_already_authenticates_is_untouched():
    authed = 'pct exec 111 -- sh -c \'curl -s -u "panel:$CPW" http://127.0.0.1:3001/api/health\''
    cmds, _, _ = mv.present_a_held_login([authed], [], [PANEL], _logins())
    assert cmds == [authed]


def test_no_login_held_changes_nothing():
    cmds, _, notes = mv.present_a_held_login([NAKED], [], [PANEL], [])
    assert cmds == [NAKED] and notes == []


def test_a_service_the_login_does_not_name_is_not_guarded():
    # the panel login, but only a radarr service measured on :3001's would-be guest:
    # the login names neither radarr's name nor unit, so nothing binds.
    other = ServiceTruth(guest="111", unit="radarr.service", name="radarr", ports=("3001",))
    cmds, _, _ = mv.present_a_held_login([NAKED], [], [other], _logins())
    assert cmds == [NAKED]


def test_a_compound_line_is_left_for_honest_failure():
    compound = NAKED + " | jq .services"
    cmds, _, _ = mv.present_a_held_login([compound], [], [PANEL], _logins())
    assert cmds == [compound]


def test_a_host_curl_not_in_the_guest_is_untouched():
    # the loopback repair wraps a host curl into its guest BEFORE this runs; a bare
    # host curl that somehow reaches here does not address the guest, so it is left.
    host = "curl -s http://127.0.0.1:3001/api/health"
    cmds, _, _ = mv.present_a_held_login([host], [], [PANEL], _logins())
    assert cmds == [host]
