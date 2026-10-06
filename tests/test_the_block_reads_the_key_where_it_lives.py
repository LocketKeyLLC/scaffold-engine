"""§17.1383 — the block reads a key where the service keeps it, and `X="$X"` sets nothing.

Both from ADD134's third draft, 2026-10-06, after §17.1382 put Jellyfin in the
registry. That draft got the hard parts right — it calls `movie/lookup` first and
takes `tmdbId` from the answer (§17.1382b's remedy, taken), it waits for the
library scan, it has no junk check. Two things still could not work:

1. The substitution only fills a `<PLACEHOLDER>` in a command or a check; a
   written FILE is deliberately left alone. So the draft wrote its own read
   inside `/tmp/chain_proof.py`, in the *arr shape::

       # Jellyfin API key -- read from the guest's config
       # Actually Jellyfin requires an API key. We'll read it from the guest.
       "grep -oP '(?<=<ApiKey>)[^<]+' /etc/jellyfin/database.xml | head -1"

   Measured: `/etc/jellyfin` is `database.xml encoding.xml logging.default.json
   logging.json network.xml system.xml`, and no `<ApiKey>` is in any of them.
   The engine held the right read and nothing put it in front of the drafter.

2. The command opened
   `TITLE="$TITLE" RADARR_API_KEY="$RADARR_API_KEY" … python3 /tmp/chain_proof.py`
   with `inputs: []`, and the program's first statement is `os.environ["TITLE"]`.
   `variables_nothing_sets` (§17.1348) returned nothing, because an assignment
   satisfies it and `TITLE="$TITLE"` IS an assignment — of the name to itself.
   The run would have searched Radarr for the empty string.
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib
import re

import pytest

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr
from app.modules.machine_values import reads_a_key_where_the_service_does_not_keep_it as WRONG_FILE
from app.modules.program_check import a_program_the_type_checker_says_will_raise
from app.modules.supervised_runs import (FILE_RULES, an_id_sent_as_zero,
                                         variables_nothing_sets)

FIX = pathlib.Path(__file__).parent / "fixtures"
#: ADD134's third draft, saved off the live engine.
LIVE = json.loads((FIX / "add134_jellyfin_key_from_the_wrong_file_2026_10_06.json").read_text())
#: the three the runner holds for this job (runner_secrets, 2026-09-30)
HELD = {"held": ["RADARR_API_KEY", "PROWLARR_API_KEY", "SONARR_API_KEY", "MASS_PASSWORD"]}


# ── the key read from a file that has none ──────────────────────────────────

def test_the_live_jellyfin_read_is_refused():
    out = WRONG_FILE(LIVE["commands"], LIVE["files"])
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "/etc/jellyfin/database.xml" in why
    assert "does not hold one" in why
    assert "ApiKeys" in why, "the remedy carries the read that works"
    assert "401" in why, "and what the empty value costs (§17.1342)"


def test_the_remedy_is_the_registry_read_itself():
    """Not a description of the fix — the command, ready to paste."""
    out = WRONG_FILE(LIVE["commands"], LIVE["files"])
    assert mv.readable_for("JELLYFIN_API_KEY").read(None) in out[0]["why"]


@pytest.mark.parametrize("line", [
    "cat /var/lib/radarr/config.xml | sed -n 's:.*<ApiKey>x</ApiKey>.*:y:p'",
    "grep -oP '(?<=<ApiKey>)[^<]+' /var/lib/sonarr/config.xml",
    "grep -oP '(?<=<ApiKey>)[^<]+' /config/config.xml",
])
def test_reading_an_arr_key_where_it_lives_is_left_alone(line):
    assert WRONG_FILE([line], []) == []


@pytest.mark.parametrize("line", [
    "grep -oP '(?<=<ApiKey>)[^<]+' /etc/plex/prefs.xml",          # not in the registry
    "cat /etc/jellyfin/network.xml",                              # no key read at all
    "python3 -c 'sqlite3 /var/lib/jellyfin/data/jellyfin.db ApiKeys'",   # the right read
])
def test_what_it_must_not_claim(line):
    assert WRONG_FILE([line], []) == []


def test_it_judges_the_code_not_the_prose():
    """§17.1040 / §17.1377 — the first cut of this gate refused FILE_RULES' own
    worked example, whose COMMENT says "/etc/jellyfin ... no <ApiKey>". A gate
    that matches the sentence warning against a defect is not reading the
    payload."""
    assert WRONG_FILE(["# /etc/jellyfin holds no <ApiKey> anywhere"], []) == []
    assert WRONG_FILE([{}.get("x") or "x = 1  # <ApiKey> in /etc/jellyfin/database.xml"], []) == []
    # and a quoted '#' is not a comment
    assert mv._strip_comment('"#!/bin/sh" # tail') == '"#!/bin/sh" '
    assert mv._strip_comment("x = 1  # c") == "x = 1  "


def test_one_refusal_per_service():
    """A program that reads the same wrong path three times is one finding."""
    body = "\n".join(["grep '<ApiKey>' /etc/jellyfin/database.xml"] * 3)
    assert len(WRONG_FILE([body], [])) == 1


# ── a variable that sets itself ─────────────────────────────────────────────

def test_the_live_self_assigned_title_is_refused():
    out = variables_nothing_sets(LIVE["commands"], LIVE["files"], HELD)
    assert len(out) == 1, [r["why"][:80] for r in out]
    assert "$TITLE" in out[0]["why"]


def test_the_runner_held_secrets_in_the_same_command_are_not_refused():
    """§17.1191 — `RADARR_API_KEY="$RADARR_API_KEY"` is the contract: the runner
    resolves it. Only the name nothing provides is named."""
    out = variables_nothing_sets(LIVE["commands"], LIVE["files"], HELD)
    # one refusal, and TITLE is its SUBJECT — the held names appear in the same
    # command (and in the remedy, as values the engine has), so their absence
    # there proves nothing; what proves it is that none of them is refused.
    assert len(out) == 1
    assert "`$TITLE` is read here" in out[0]["why"]


@pytest.mark.parametrize("cmd", [
    'TITLE="$TITLE" python3 /x.py',
    "TITLE=$TITLE python3 /x.py",
    'TITLE="${TITLE}" python3 /x.py',
])
def test_every_spelling_of_the_self_assignment(cmd):
    assert len(variables_nothing_sets([cmd], [], {})) == 1


@pytest.mark.parametrize("cmd", [
    'TITLE="Sintel" python3 /x.py',                    # a value
    'K=$(cat /x) ; curl -H "K: $K" http://y',          # read from somewhere
    'TITLE="$OTHER" python3 /x.py',                    # another name is a different question
])
def test_a_real_assignment_is_not_this_defect(cmd):
    out = variables_nothing_sets([cmd], [], {})
    assert all("$TITLE" not in r["why"] for r in out), out


def test_a_held_secret_self_assigned_is_still_fine():
    assert variables_nothing_sets(
        ['RADARR_API_KEY="$RADARR_API_KEY" python3 /x.py'], [], HELD) == []


# ── wired where they have to be ─────────────────────────────────────────────

def test_the_wrong_file_gate_runs_in_frame_run():
    src = inspect.getsource(sr.frame_run)
    assert "_mv.reads_a_key_where_the_service_does_not_keep_it(cmds, shape_files)" in src


def test_the_self_assignment_is_discounted_inside_the_existing_gate():
    """Not a new gate: §17.1348 already asks "does anything set this?" and was
    answering yes to a name that sets itself."""
    src = inspect.getsource(sr.variables_nothing_sets)
    assert "_SELF_ASSIGN_RE" in src


def test_both_refusals_drive_a_redraft():
    for refused in (WRONG_FILE(LIVE["commands"], LIVE["files"]),
                    variables_nothing_sets(LIVE["commands"], LIVE["files"], HELD)):
        assert refused
        assert sr.shape_retry_note({"kind": "run", "refused": refused})


# ── the rules teach the read, and the rules' own examples hold up ───────────

def test_the_rules_teach_both_shapes():
    assert "A SERVICE'S API KEY IS READ WHERE THAT SERVICE KEEPS IT" in FILE_RULES
    assert "<JELLYFIN_API_KEY>" in FILE_RULES
    assert "a ROW in its own SQLite database" in FILE_RULES
    assert "Never grep `<ApiKey>` out of a Jellyfin file" in FILE_RULES


def test_every_python_example_in_the_rules_parses():
    """§17.1377's lesson with the hole closed. The type-check gate deliberately
    says NOTHING about a program it cannot parse — so a broken example is
    invisible to it, and the first cut of the rule above was broken Python
    (nested double quotes the surrounding literal had already consumed). Parsing
    is asserted here, separately."""
    blocks = re.findall(r"```python\n(.*?)```", FILE_RULES, re.S)
    assert len(blocks) >= 5
    for i, b in enumerate(blocks):
        ast.parse(b)          # raises SyntaxError, naming the block, if it is broken


def test_every_python_example_in_the_rules_passes_every_new_gate():
    blocks = re.findall(r"```python\n(.*?)```", FILE_RULES, re.S)
    for i, b in enumerate(blocks):
        assert a_program_the_type_checker_says_will_raise([(f"rule{i}.py", b)]) == [], b
        assert an_id_sent_as_zero([b], []) == [], b
        assert WRONG_FILE([b], []) == [], b


def test_the_rules_own_arr_read_renders_as_working_sed():
    """`FILE_RULES` is not a raw string, so a single backslash in an example is
    an invalid escape — and the drafter copies what it RENDERS to, not what the
    source says."""
    line = next(l for l in FILE_RULES.splitlines() if "ARR_KEY_CMD" in l)
    assert r"s:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p" in line


def test_the_fixture_is_the_live_draft_and_carries_no_key():
    assert LIVE["commands"] and LIVE["files"]
    blob = json.dumps(LIVE)
    assert "chain_proof.py" in blob
    assert "AccessToken FROM ApiKeys" not in blob, "the draft's own wrong read, not ours"
