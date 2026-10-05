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


# ---------------------------- §17.1367b — an extracted scalar is not a document


ID_DRAFT = [
    'RADARR_ID=$(echo "$RADARR_CLIENT" | python3 -c "import sys,json; '
    'clients=json.load(sys.stdin); print(clients[0][\'id\'] if clients else 1)")',
    'pct exec 103 -- sh -c "curl -s --fail-with-body -X PUT -H \'X-Api-Key: $RADARR_KEY\' '
    '-d \'{\\"id\\":$RADARR_ID,\\"name\\":\\"qBittorrent\\"}\' '
    'http://127.0.0.1:7878/api/v3/downloadclient/$RADARR_ID"',
]


def test_an_extracted_id_is_not_a_document():
    r"""Live, 2026-10-05: this gate refused a CORRECT draft. `$RADARR_ID` is one
    integer the block pulled out with `clients[0]['id']` and wrote at a bare JSON
    numeric position — not the 6,182-byte object the rule exists for. The
    discriminator is what the substitution PRODUCES: `json.dumps`, a bare `curl`,
    a `cat` of a file with nothing piped after it."""
    assert a_machine_value_in_a_shell_word(ID_DRAFT) == []


def test_the_document_producers_are_the_ones_that_count():
    from app.modules.supervised_runs import _WHOLE_DOCUMENT_RE as W
    for producer in ("echo x | python3 -c 'print(json.dumps(c))'",
                     "curl -s http://127.0.0.1:7878/api/v3/downloadclient",
                     "cat /var/lib/radarr/body.json"):
        assert W.search(producer), producer
    for extraction in ("cat /var/lib/radarr/config.xml | sed -n 's:x:y:p' | head -n 1",
                       "python3 -c \"print(clients[0]['id'])\"",
                       "pct exec 103 -- sh -c 'cat /x | sed -n 1p'"):
        assert not W.search(extraction), extraction


def test_the_substitution_body_stops_at_its_own_paren():
    r"""A fixed window is wrong both ways: too short misses a `json.dumps` several
    lines down inside `python3 -c '…'`; long enough to catch it runs into the NEXT
    command, where a bare `curl` makes every extracted id look like a document."""
    from app.modules.supervised_runs import _substitution_body
    text = "V=$(echo a | python3 -c 'print(json.dumps(x))')\ncurl -s http://h:1/x\n"
    body = _substitution_body(text, text.index("$(") + 2)
    assert "json.dumps" in body
    assert "curl" not in body


# ------------------- §17.1374 — the body is whatever sits between -d and the URL


JUGGLED = json.loads((pathlib.Path(__file__).parent / "fixtures"
                      / "add132_quote_juggled_body_2026_10_05.json").read_text())


def test_the_quote_juggled_body_is_still_a_body():
    r"""Live, 2026-10-05. The draft fetched the whole download-client object —
    correct, it carries `configContract` along — and then wrote:

        -d '"'"''"$RADARR_UPDATED"''"'"' http://127.0.0.1:7878/api/v3/downloadclient/1

    which resolves, for the inner shell, to `-d '<the whole JSON>'`. That is
    §17.1367's failure exactly, and a pattern demanding the variable sit inside
    ONE quoted word saw nothing: the quote juggling splits it across four.

    The body is simply whatever lies between the `-d` flag and the URL.
    """
    out = a_machine_value_in_a_shell_word(JUGGLED["commands"], JUGGLED["files"])
    assert len(out) == 2, [r["why"][:70] for r in out]
    names = " ".join(r["why"] for r in out)
    assert "$RADARR_UPDATED" in names and "$SONARR_UPDATED" in names


def test_the_body_span_stops_at_the_url():
    """An id in the URL is not in the body — that is the §17.1367b exemption, and
    a span that ran to the end of the line would swallow it."""
    from app.modules.supervised_runs import _BODY_SPAN_RE
    line = "curl -X PUT -d '{\"a\":1}' http://127.0.0.1:7878/api/v3/downloadclient/$RADARR_ID"
    spans = [m.group("body") for m in _BODY_SPAN_RE.finditer(line)]
    assert spans and "$RADARR_ID" not in spans[0], spans


def test_a_continued_curl_is_read_as_one_command_here_too():
    """§17.1373 — the scan reads logical lines, so a body on a continuation line
    still belongs to the `-d` above it."""
    lines = ["V=$(curl -s http://h:1/x)",
             "curl -X PUT \\\n    -d \"$V\" \\\n    http://127.0.0.1:7878/api/v3/x"]
    assert a_machine_value_in_a_shell_word(lines)


def test_the_id_draft_is_still_accepted_after_the_widening():
    """The whole point of §17.1367b: an extracted scalar is not a document."""
    earlier = json.loads((pathlib.Path(__file__).parent / "fixtures"
                          / "add132_multiline_curl_2026_10_05.json").read_text())
    assert a_machine_value_in_a_shell_word(earlier["commands"], earlier["files"]) == []


# ------------- §17.1375 — the drafter is told the shape, not only refused


def test_the_file_rules_teach_the_body_shape_up_front():
    r"""Four entries (§17.1367, §17.1368, §17.1373, §17.1374) caught four quoting
    variants of the same mistake, and the drafter only ever learned the remedy
    AFTER being refused — the rules it reads every time said nothing about how to
    send a body. A gate that refuses without the rule teaching the shape is a loop.
    """
    from app.modules.supervised_runs import FILE_RULES
    assert "A REQUEST BODY IS A FILE, NEVER A SHELL WORD" in FILE_RULES
    # the measurement, so the rule carries its own evidence
    assert "6,182 bytes" in FILE_RULES and "three apostrophes" in FILE_RULES
    assert 'Syntax error: "(" unexpected' in FILE_RULES
    # both shapes that work, and the better one
    assert "-d @/tmp/body.json" in FILE_RULES
    assert "-d @-" in FILE_RULES
    assert "urllib.request" in FILE_RULES and "data=json.dumps" in FILE_RULES


def test_the_rule_and_the_refusal_name_the_same_remedies():
    """A refusal that names a shape the rules do not teach is how the loop
    persisted; they must agree."""
    from app.modules.supervised_runs import FILE_RULES
    out = a_machine_value_in_a_shell_word(LIVE)
    assert out
    why = out[0]["why"]
    for shape in ("-d @-", "@/path"):
        assert shape in why, shape
    assert "-d @-" in FILE_RULES
