r"""§17.1367 — a value read from a machine, pasted into a shell word.

Live, 2026-10-04. ADD132's eighth draft did exactly what §17.1366 carried to it:
GET the download client, change two fields, PUT the whole object back. It
fetched the object into a variable and then interpolated it:

    RADARR_UPDATED=$(echo "$RADARR_CLIENT" | python3 -c '…json.dumps(c)…')
    pct exec 103 -- sh -c "curl … -d '$RADARR_UPDATED' http://…/downloadclient/1"

and died:

    qBittorrent login: Ok.
    sh: 1: Syntax error: "(" unexpected

Reproduced with real `sh` against the real object
(`tests/fixtures/radarr_downloadclient_1_2026_10_04.json`, 6,182 bytes):

    apostrophes: 3   parens: 6
    sh -n on the run-time shape:  sh: 210: Syntax error: "(" unexpected
    line 32: "helpText": "… See Options -> Web UI -> 'Use HTTPS instead of HTTP' …"

qBittorrent's own help text, inside Radarr's client object, carries apostrophes.
The first closes the single-quoted `-d '…'`, and a later `(` from `"hint": "(0)"`
then parses as shell syntax. **No escaping fixes it, because the block does not
know what the value contains: it came from the machine.** Such a value travels on
a channel with no quoting — stdin (`-d @-` and a pipe) or a file (`-d @/path`).

§17.1255/1257 compile what a block hands to another interpreter, and this text
compiles: the break exists only once the variable expands. So the judgment is
about PROVENANCE, not syntax.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

from app.modules.supervised_runs import (a_machine_value_in_a_shell_word,
                                         refusal_kinds)

CLIENT = (pathlib.Path(__file__).parent / "fixtures"
          / "radarr_downloadclient_1_2026_10_04.json").read_text().strip()
#: the live shape, reduced to its two decisive lines
LIVE = [
    'RADARR_UPDATED=$(echo "$RADARR_CLIENT" | python3 -c \'\nimport json, sys, os\n'
    'c = json.load(sys.stdin)\nc["password"] = os.environ["MASS_PASSWORD"]\n'
    'print(json.dumps(c))\n\')',
    'RADARR_RESULT=$(pct exec 103 -- sh -c "curl -s -X PUT -H \\"X-Api-Key: $RADARR_KEY\\" '
    '-H \\"Content-Type: application/json\\" -d \'$RADARR_UPDATED\' '
    'http://127.0.0.1:7878/api/v3/downloadclient/1")',
]


# ------------------------------------------- the real object breaks a real shell


def test_the_real_object_carries_what_breaks_a_quoted_word():
    assert len(CLIENT) > 6000
    assert CLIENT.count("'") == 3, CLIENT.count("'")
    assert CLIENT.count("(") == 6
    assert "'Use HTTPS instead of HTTP'" in CLIENT


def test_sh_itself_refuses_the_run_time_shape():
    """Not reasoned — run. This is the exact error the live step died with."""
    payload = ("curl -s -X PUT -H \"X-Api-Key: K\" -d '" + CLIENT
               + "' http://127.0.0.1:7878/api/v3/downloadclient/1")
    r = subprocess.run(["sh", "-n", "-c", payload], capture_output=True, text=True)
    assert r.returncode != 0
    assert "unexpected" in r.stderr, r.stderr


def test_the_remedies_really_work_in_a_real_shell():
    """Both channels the refusal names carry the same bytes intact."""
    for script in (
        "printf '%s' \"$BODY\" | sh -c 'cat | wc -c'",
        "printf '%s' \"$BODY\" > \"$TMP\"; sh -c 'wc -c < \"$TMP\"'",
    ):
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env={"BODY": CLIENT, "TMP": "/tmp/_1367_body.json", "PATH": "/usr/bin:/bin"})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(len(CLIENT)), (r.stdout, len(CLIENT))


# ------------------------------------------------------------- the gate


def test_the_live_lines_are_refused():
    out = a_machine_value_in_a_shell_word(LIVE)
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "`$RADARR_UPDATED` holds whatever" in why
    assert "one wrong character ends the quote" in why
    assert "3 apostrophes" in why and "hint" in why          # the measurement
    assert "-d @-" in why and "-d @/path/to/file" in why     # the remedies


def test_a_header_is_not_a_body():
    """`-H "X-Api-Key: $KEY"` carries a short controlled value read off a config
    file — the shape every correct draft uses. Flagging it refuses real work."""
    lines = ['KEY=$(pct exec 103 -- sh -c \'sed -n "s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p" /x\')',
             'pct exec 103 -- sh -c "curl -s -H \\"X-Api-Key: $KEY\\" http://127.0.0.1:7878/api/v3/health"']
    assert a_machine_value_in_a_shell_word(lines) == []


def test_arithmetic_is_not_a_machine():
    """`deadline=$(( SECONDS + 165 ))` is the engine's own wait loop, in six of its
    own templates. The first cut of this flagged seven blocks for it."""
    lines = ["deadline=$(( SECONDS + 165 ))",
             'curl -d "$deadline" http://x']
    assert a_machine_value_in_a_shell_word(lines) == []


def test_a_value_the_block_wrote_itself_is_fine():
    lines = ['BODY=\'{"id":1}\'', 'pct exec 103 -- sh -c "curl -d \'$BODY\' http://x"']
    assert a_machine_value_in_a_shell_word(lines) == []


def test_stdin_and_a_file_are_accepted():
    for ok in ('printf \'%s\' "$V" | pct exec 103 -- sh -c "curl -d @- http://x"',
               'pct exec 103 -- sh -c "curl -d @/tmp/body.json http://x"'):
        assert a_machine_value_in_a_shell_word(["V=$(curl -s http://y)", ok]) == [], ok


def test_a_multi_line_substitution_is_still_a_machine_value():
    """The one that broke: its `$(` closes six lines later, and requiring the
    paren on the same line missed it entirely."""
    out = a_machine_value_in_a_shell_word(LIVE)
    assert out and "RADARR_UPDATED" in out[0]["why"]


def test_one_refusal_per_name():
    doubled = LIVE + LIVE
    assert len(a_machine_value_in_a_shell_word(doubled)) == 1


def test_the_files_are_read_too():
    assert a_machine_value_in_a_shell_word([], [{"path": "/tmp/x.sh",
                                                 "content": "\n".join(LIVE)}])


def test_the_refusal_asks_the_drafter_again():
    out = a_machine_value_in_a_shell_word(LIVE)
    assert "pasted inside a quoted shell word" in refusal_kinds({"refused": out})


def test_the_framer_runs_it():
    import inspect

    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.frame_run)
    assert "a_machine_value_in_a_shell_word(cmds, shape_files)" in src


def test_the_fixture_holds_no_secret():
    """The *arr API returns the password masked, which is why this object is safe
    to keep — and is itself worth knowing: PUTting it back unchanged would send
    `********` as the password."""
    d = json.loads(CLIENT)
    by = {f["name"]: f.get("value") for f in d.get("fields", [])}
    assert by.get("password") == "********"
    assert by.get("username") == "admin"
    assert not re.search(r"@ByteArray\(", CLIENT)
