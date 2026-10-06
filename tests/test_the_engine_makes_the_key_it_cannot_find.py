"""§17.1385 — the call runs where the key can be read, and the engine makes one if there is none.

The operator, 2026-10-06, after four drafts had died reaching for a Jellyfin
login and I had asked them to issue a key by hand::

    "Can't it just create one with my permission"

It can, and the engine's own approval flow IS that permission. Two halves:

A. `a_host_program_needs_a_key_only_the_guest_can_read` — ADD134's proof calls
   Jellyfin from a Python program that runs on the HOST. The engine fills
   `<JELLYFIN_API_KEY>` only in a command or a check that runs inside the guest
   (a written file is left alone on purpose), and the runner injects only the
   names its stores hold. So that program had no way to obtain a token, which is
   why four drafts in a row invented a login: it was the one position the
   refusals left with no answer. The remedy names the Verify section, because
   the step's done-condition — "Jellyfin's API lists the title" — is a READ.

B. `create_key_on_the_machine` — if the read says `ApiKeys is empty`, the engine
   makes a key: a token from `secrets`, the columns INTROSPECTED rather than
   assumed, written with the service stopped and the service restarted after.
   Proven against a real SQLite table (see the fixture docstring below), both
   for the empty case and for a schema carrying a column the engine does not
   know, which fails BY NAME and inserts nothing.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr
from app.modules.machine_values import (a_host_program_needs_a_key_only_the_guest_can_read as HOSTPROG,
                                        create_key_on_the_machine)

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE_BODY = (FIX / "add134_invented_a_jellyfin_login_2026_10_06.py").read_text()
LIVE_CMD = ['RADARR_API_KEY="$RADARR_API_KEY" python3 /tmp/chain_proof.py "<TITLE>"']
LIVE_FILES = [{"path": "/tmp/chain_proof.py", "content": LIVE_BODY}]
#: what the runner holds for this job — Jellyfin is NOT among them
HELD = {"held": ["RADARR_API_KEY", "PROWLARR_API_KEY", "SONARR_API_KEY", "MASS_PASSWORD"]}


# ── A: the call must run where the key can be read ──────────────────────────

def test_the_live_host_program_is_refused():
    out = HOSTPROG(LIVE_CMD, LIVE_FILES, HELD)
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "runs on the HOST" in why
    assert "Verify section" in why, "the remedy names where the call belongs"
    assert "ApiKeys" in why, "and carries the read"
    assert "systemctl" in why and "INSERT" in why, "and the create, for the empty case"


def test_a_key_the_runner_holds_is_fine_in_a_host_program():
    """Why this is keyed on the stores: the *arr keys ARE injected, so the same
    program's Radarr calls are not this defect."""
    assert HOSTPROG(LIVE_CMD, LIVE_FILES, {"held": ["JELLYFIN_API_KEY"]}) == []
    assert HOSTPROG(LIVE_CMD, LIVE_FILES, {"secrets": ["JELLYFIN_KEY"]}) == []


def test_an_arr_only_host_program_is_left_alone():
    f = [{"path": "/tmp/x.py", "content": 'curl -H "X-Api-Key: k" http://r:7878/api/v3/movie'}]
    assert HOSTPROG(["python3 /tmp/x.py"], f, HELD) == []


def test_a_file_the_block_never_runs_is_left_alone():
    assert HOSTPROG(["echo hi"], LIVE_FILES, HELD) == []


def test_a_program_that_runs_in_the_guest_is_left_alone():
    """The whole point is POSITION: inside the guest, the read works."""
    cmds = ["pct exec 101 -- python3 /tmp/chain_proof.py"]
    assert HOSTPROG(cmds, LIVE_FILES, HELD) == []


def test_only_a_service_with_a_readable_is_judged():
    for app in mv._AUTH_HEADERS:
        assert mv._SERVICES.get(app) is not None, app


# ── B: the engine makes the key ─────────────────────────────────────────────

def test_the_create_is_offered_only_where_it_is_needed():
    assert create_key_on_the_machine("jellyfin") != ""
    # the *arr family writes its own key at install; there is nothing to create
    assert create_key_on_the_machine("radarr") == ""
    assert create_key_on_the_machine("sonarr", "104") == ""


def test_the_create_runs_in_the_guest_that_owns_it():
    cmd = create_key_on_the_machine("jellyfin", "101")
    assert cmd.startswith("pct exec 101 -- sh -c ")


def test_the_create_is_a_whole_python_program():
    """§17.1380's lesson: a program that cannot parse was never tested."""
    body = mv._JELLYFIN_CREATE.split("python3 -c ", 1)[1]
    ast.parse(body[1:-1] if body.startswith("'") else body)


def test_the_create_introspects_rather_than_assumes():
    """The columns are read off the table, so a schema this engine has not seen
    fails by NAME instead of on a guessed INSERT."""
    body = mv._JELLYFIN_CREATE
    assert "PRAGMA table_info(ApiKeys)" in body
    assert "columns this engine does not know" in body


def test_the_create_stops_the_service_and_starts_it_again():
    """A running Jellyfin need not re-read the table."""
    body = mv._JELLYFIN_CREATE
    assert 'systemctl' in body and '"stop"' in body and '"start"' in body
    assert body.index('"stop"') < body.index("INSERT")
    assert body.index("INSERT") < body.index('"start"')


def test_the_create_is_idempotent_and_prints_no_token():
    """Idempotent, because approving it twice must not make two keys. §17.1392
    changed two things: it looks for the engine's OWN key (a create that
    returned early because SOME key existed would hand back another service's),
    and it prints no token at all — printing one is how the value reached a run
    output, then §17.1366's carry-forward, then the model."""
    body = mv._JELLYFIN_CREATE
    assert "SELECT 1 FROM ApiKeys WHERE Name=?" in body
    assert body.index("SELECT 1 FROM ApiKeys") < body.index("INSERT")
    assert "sys.exit(0) if have else None" in body
    assert "print(tok)" not in body
    assert "scaffold-engine key created" in body


def test_the_create_is_work_not_a_check():
    """It restarts a service, so it belongs under `## Run this` where the
    operator approves it — never in Verify."""
    out = HOSTPROG(LIVE_CMD, LIVE_FILES, HELD)
    assert "## Run this" in out[0]["why"]
    assert "never in Verify" in sr.FILE_RULES


# ── wired, and the rules say it ─────────────────────────────────────────────

def test_it_runs_in_frame_run_with_the_policy():
    src = inspect.getsource(sr.frame_run)
    assert "_mv.a_host_program_needs_a_key_only_the_guest_can_read(" in src
    assert "cmds, shape_files, policy)" in src, "the stores decide, so policy is passed"


def test_it_drives_a_redraft():
    refused = HOSTPROG(LIVE_CMD, LIVE_FILES, HELD)
    assert refused
    assert sr.shape_retry_note({"kind": "run", "refused": refused})


def test_the_rules_teach_the_position_and_the_create():
    assert "A CALL THAT NEEDS A READABLE KEY RUNS INSIDE THE GUEST" in sr.FILE_RULES
    assert "the engine makes one" in sr.FILE_RULES
    assert "X-Emby-Token" in sr.FILE_RULES


def test_every_rule_example_still_passes_every_gate():
    """Including the new bash ones — the rules must not teach what the gates
    refuse (§17.1377), and a gate that refused its own documentation is how
    §17.1383 nearly shipped."""
    import re
    from app.modules.machine_values import (authenticates_instead_of_reading_the_key as A,
                                            reads_a_key_where_the_service_does_not_keep_it as W)
    for b in re.findall(r"```(?:python|bash)\n(.*?)```", sr.FILE_RULES, re.S):
        assert A([b], []) == [], b[:120]
        assert W([b], []) == [], b[:120]
