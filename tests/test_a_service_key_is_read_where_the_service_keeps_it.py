"""§17.1382 — a service's API key is read where THAT service keeps it.

The operator, 2026-10-03, on the home-lab job::

    "The api keys for radarr and sonarr the engine should be able to retrieve
     itself, it did so with PRowlarr."

§17.1332 fixed that — as a FAMILY. `_ARR_APPS`, one `config.xml`, one `<ApiKey>`
element, one `sed`. Then 2026-10-06, the same operator on the same job::

    "Why cant the engine achieve all of this?? Everything you are asking for is
     it has access to and should be able to retrieve itself. This issue happened
     with radar sonar and prowlarr as well"

ADD134's draft needed Jellyfin's key, Jellyfin keeps its keys as rows in its own
SQLite database rather than in a config file, so it fell straight through the
family and landed back on the operator as an input — and then
`verify_needs_a_value_the_run_never_used` DROPPED the only check that tested the
step's stated done-condition ("Jellyfin's API lists the title"), because the
placeholder appeared nowhere else.

Measured on guest 101 before building this: `/bin/python3` is present (its
`sqlite3` is in the standard library, so nothing needs installing), the `ApiKeys`
table and its `AccessToken` column are in `jellyfin.db`, and the file is
world-readable while the runner is root. The key was there to read the whole
time. A registry is the shape that generalises: a service is an entry, and
`read_on_the_machine` and `still_asked` need no change to gain one.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr
from app.modules.machine_values import (Readable, read_on_the_machine, readable_for,
                                        still_asked)
from app.modules.supervised_runs import (a_check_that_proves_nothing,
                                         an_id_sent_as_zero)

FIX = pathlib.Path(__file__).parent / "fixtures"
#: ADD134's real parked frame, saved off the live engine.
LIVE = json.loads((FIX / "add134_proves_nothing_and_a_zero_id_2026_10_06.json").read_text())
#: what `pct list` says on this host
INVENTORY = {"names": {"101": "jellyfin", "102": "prowlarr", "103": "radarr",
                       "104": "sonarr", "105": "download-client"}}


# ── the registry ─────────────────────────────────────────────────────────────

def test_jellyfin_is_a_service_the_engine_can_read():
    r = readable_for("JELLYFIN_API_KEY")
    assert r is not None and r.app == "jellyfin"


@pytest.mark.parametrize("name,app", [
    ("JELLYFIN_API_KEY", "jellyfin"), ("JELLYFIN_KEY", "jellyfin"),
    ("JELLYFIN_APIKEY", "jellyfin"), ("JELLYFIN_TOKEN", "jellyfin"),
    ("RADARR_API_KEY", "radarr"), ("SONARR_APIKEY", "sonarr"),
    ("PROWLARR_KEY", "prowlarr"),
])
def test_the_names_the_drafter_actually_writes(name, app):
    r = readable_for(name)
    assert r is not None and r.app == app


@pytest.mark.parametrize("name", ["RANDOM_KEY", "MASS_PASSWORD", "TITLE", "API_KEY", ""])
def test_a_name_no_service_owns_is_left_alone(name):
    assert readable_for(name) is None


def test_adding_a_service_is_one_registry_entry():
    """The point of the fix: the family became a registry, so the NEXT service
    with its own storage shape does not land back on the operator."""
    assert "jellyfin" in mv._SERVICES
    assert set(mv._ARR_APPS) <= set(mv._SERVICES)
    for app, r in mv._SERVICES.items():
        assert isinstance(r, Readable) and r.app == app
        assert r.extract or r.command, app


# ── the Jellyfin read itself ─────────────────────────────────────────────────

def test_the_jellyfin_read_opens_the_database_where_it_lives():
    """A `cat … | filter` cannot read a database: the whole read goes into the
    guest, unlike the *arr form whose filter runs on the host."""
    r = readable_for("JELLYFIN_API_KEY")
    inside = r.read("101")
    assert inside.startswith("pct exec 101 -- sh -c ")
    assert "python3 -c" in inside
    assert "ApiKeys" in inside and "AccessToken" in inside


def test_the_jellyfin_read_is_read_only_and_needs_nothing_installed():
    """Measured on guest 101: `/bin/python3` is there and its sqlite3 is in the
    standard library. `mode=ro` so a running server is never locked."""
    body = readable_for("JELLYFIN_API_KEY").read(None)
    assert "mode=ro" in body and "uri=True" in body
    assert "python3 -c" in body
    assert "apt" not in body and "install" not in body
    assert "sqlite3 /" not in body, "the sqlite3 BINARY is not installed on the guest"


def test_the_jellyfin_read_fails_loudly_when_there_is_no_key():
    """§17.1342's measured failure: an empty value goes out as an empty header
    and comes back 401, which reads as a WRONG key rather than a missing one."""
    body = readable_for("JELLYFIN_API_KEY").read(None)
    assert "sys.exit(" in body
    assert "ApiKeys is empty" in body


def test_the_arr_read_is_unchanged():
    """A generalisation that moved the *arr family's behaviour would be a
    regression dressed as a fix."""
    body = readable_for("RADARR_API_KEY").read(None)
    assert body == ("cat /var/lib/radarr/config.xml /config/config.xml 2>/dev/null | "
                    r'sed -n "s:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p" | head -n 1')


# ── end to end: the check that was being dropped ────────────────────────────

JELLYFIN_CHECK = ('pct exec 101 -- bash -c \'curl -s '
                  '"http://127.0.0.1:8096/Items?api_key=<JELLYFIN_API_KEY>&searchTerm=Sintel"\'')


def test_the_jellyfin_check_is_filled_instead_of_dropped():
    """The live defect, end to end. `read_on_the_machine` runs BEFORE the drop
    rule in `frame_run`, so once Jellyfin is in the registry the placeholder is
    gone by the time §17.1288's drop rule looks — and the only check that tested
    the step's done-condition survives."""
    cmds, verify, notes = read_on_the_machine(["pct exec 103 -- sh -c 'true'"],
                                              [JELLYFIN_CHECK], INVENTORY)
    assert "<JELLYFIN_API_KEY>" not in verify[0]
    assert "ApiKeys" in verify[0]
    assert notes and "guest 101" in notes[0]["why"]
    # and with the placeholder gone, nothing is left for the drop rule to drop
    assert sr.verify_needs_a_value_the_run_never_used(cmds, verify) == []


def test_the_placeholder_was_what_the_drop_rule_saw():
    """Vacuity guard: before the fix this check WAS dropped, so the test above
    is testing the real thing."""
    dropped = sr.verify_needs_a_value_the_run_never_used(
        ["pct exec 103 -- sh -c 'true'"], [JELLYFIN_CHECK])
    assert len(dropped) == 1
    assert "JELLYFIN_API_KEY" in dropped[0]["why"]


def test_asking_the_operator_for_it_is_refused():
    out = still_asked([{"name": "JELLYFIN_API_KEY", "secret": True}], INVENTORY)
    assert len(out) == 1
    assert "not a question for the operator" in out[0]["why"]
    assert "guest 101" in out[0]["why"]


def test_an_unnamed_guest_leaves_it_an_input():
    """Blindness invents nothing (§17.1289): with no jellyfin in the inventory
    the value stays an operator input rather than reading the wrong machine."""
    assert still_asked([{"name": "JELLYFIN_API_KEY"}], {"names": {"103": "radarr"}}) == []
    _, verify, notes = read_on_the_machine([], [JELLYFIN_CHECK], {"names": {}})
    assert verify == [JELLYFIN_CHECK] and notes == []


def test_a_check_that_does_not_run_in_the_guest_is_left_alone():
    """§17.1342 — a `pct exec` inside `$( … )` on the host runs unprivileged and
    returns nothing, so a host-side call keeps the placeholder."""
    host = 'curl -s "http://192.168.1.20:8096/Items?api_key=<JELLYFIN_API_KEY>"'
    _, verify, notes = read_on_the_machine([], [host], INVENTORY)
    assert verify == [host] and notes == []


# ── §17.1382: a check that proves nothing ───────────────────────────────────

def test_the_live_bare_id_check_is_refused():
    out = a_check_that_proves_nothing(LIVE["verify"])
    assert len(out) == 1, [r["command"] for r in out]
    assert out[0]["command"] == "id"
    assert "answers the same thing on every machine" in out[0]["why"]


@pytest.mark.parametrize("cmd", ["id", "true", "pwd", "whoami", "date", "hostname", "uptime"])
def test_every_zero_information_command_is_refused(cmd):
    assert len(a_check_that_proves_nothing([cmd])) == 1


@pytest.mark.parametrize("cmd", [
    "id -u jellyfin",
    'echo "$(curl -s http://127.0.0.1:8096/Items)"',
    "pct exec 105 -- bash -c 'ls -la /media/downloads/'",
    "systemctl is-active jellyfin",
])
def test_a_check_that_reads_something_is_left_alone(cmd):
    assert a_check_that_proves_nothing([cmd]) == []


def test_no_checks_is_not_this_gates_business():
    """§17.1345 owns an empty verify; this gate judges the checks that exist."""
    assert a_check_that_proves_nothing([]) == []
    assert a_check_that_proves_nothing(["", "   "]) == []


# ── §17.1382b: an id sent as zero ───────────────────────────────────────────

def test_the_live_zero_tmdbid_is_refused():
    out = an_id_sent_as_zero(LIVE["commands"], [])
    assert len(out) == 1, out
    assert "`tmdbId` is sent as 0" in out[0]["why"]
    assert "movie/lookup" in out[0]["why"], "the remedy names where the value comes from"


@pytest.mark.parametrize("body", [
    r'curl -d "{\"tmdbId\":0}" http://x',          # escaped, as it arrives live
    """curl --data '{"tmdbId": 0}' http://x""",     # plain
    """json.dumps({"movieId": 0})""",               # built in a program
])
def test_both_spellings_of_the_body_are_read(body):
    """§17.1048 — a gate matches modulo formatting, or it does not match the
    thing that happens. The first cut of this pattern required a bare quote and
    saw nothing in the live body."""
    assert len(an_id_sent_as_zero([body], [])) == 1


@pytest.mark.parametrize("body", [
    """curl -d '{"tmdbId":45745}' http://x""",      # a real id
    """curl -d '{"tmdbId":0.5}' http://x""",        # not an integer zero
    """curl -d '{"qualityProfileId":1}' http://x""",
    "curl -s http://x/api/v3/movie | grep tmdbId",  # a RESPONSE, not a body
    """curl -d '{"year":0}' http://x""",            # not an id field
])
def test_what_it_must_not_claim(body):
    assert an_id_sent_as_zero([body], []) == []


def test_a_file_the_block_writes_is_judged_too():
    f = [{"path": "/tmp/add.py", "content": 'import json\njson.dumps({"tmdbId": 0})\n'}]
    assert len(an_id_sent_as_zero([], f)) == 1


# ── wired where they have to be ─────────────────────────────────────────────

def test_both_gates_run_in_frame_run():
    src = inspect.getsource(sr.frame_run)
    assert "a_check_that_proves_nothing(verify)" in src
    assert "an_id_sent_as_zero(cmds, shape_files)" in src


def test_both_refusals_drive_a_redraft():
    """§17.1269 — unregistered, each would park the frame with Run greyed out."""
    for refused in (a_check_that_proves_nothing(LIVE["verify"]),
                    an_id_sent_as_zero(LIVE["commands"], [])):
        assert refused
        assert sr.shape_retry_note({"kind": "run", "refused": refused})


def test_the_fixture_is_the_live_frame_and_carries_no_secret():
    assert LIVE["commands"] and LIVE["verify"]
    blob = json.dumps(LIVE)
    assert "<TITLE>" in blob, "the frame's own placeholders, not a filled-in value"
    for leak in ("ApiKey>", "MASS_PASSWORD="):
        assert leak not in blob
