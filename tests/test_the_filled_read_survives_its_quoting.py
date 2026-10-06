"""§17.1386 — the read the engine pastes must survive the quotes around it.

Live, 2026-10-06. The narrowed ADD134 frame was perfect: `refused: 0`,
`inputs: 0`, the drafter wrote `<JELLYFIN_API_KEY>` and §17.1332's substitution
filled it. The run died instantly::

    bash: -c: line 1: syntax error near unexpected token `('

The engine had pasted `$(python3 -c 'import sqlite3,…')` into

    pct exec 101 -- sh -c 'curl … -H "X-Emby-Token: <JELLYFIN_API_KEY>" …'

and the read's first single quote CLOSED that payload. §17.1342 avoided this by
writing the *arr extract with double quotes only — a discipline that cannot
survive a read which is a program with its own string literals, and §17.1382
added exactly such a read without noticing the invariant in the comment two
lines above it.

So the quoting is settled at the substitution site, once, for every service.
And it is checked with `bash -n` rather than by reading the string, because
every gate in this file is about a shape a shell has to accept
([[feedback_shell_shapes_need_bash_level_tests]]).
"""
from __future__ import annotations

import subprocess

import pytest

from app.modules.machine_values import _for_context, read_on_the_machine, readable_for

INV = {"names": {"101": "jellyfin", "103": "radarr"}}


def _bash_n(cmd: str) -> tuple[int, str]:
    r = subprocess.run(["bash", "-n"], input=cmd, text=True, capture_output=True)
    return r.returncode, (r.stderr or "").strip()


#: the live frame's own command, with the placeholder where the drafter put it
LIVE = ("pct exec 101 -- sh -c 'curl -s -H \"X-Emby-Token: <JELLYFIN_API_KEY>\" "
        "\"http://127.0.0.1:8096/Items?Recursive=true&SearchTerm=Sintel\"'")


def test_the_live_filled_command_is_accepted_by_bash():
    """THE regression: this exact shape exited 2 before a single byte of work."""
    cmds, _verify, notes = read_on_the_machine([LIVE], [], INV)
    assert "<JELLYFIN_API_KEY>" not in cmds[0] and notes
    rc, err = _bash_n(cmds[0])
    assert rc == 0, f"{err}\n\n{cmds[0]}"


def test_the_unescaped_read_really_would_have_broken_it():
    """Vacuity guard: without the escaping the command is a syntax error, so the
    test above is testing the fix and not a shape that was always fine."""
    raw = LIVE.replace("<JELLYFIN_API_KEY>", f"$({readable_for('JELLYFIN_API_KEY').read(None)})")
    rc, err = _bash_n(raw)
    assert rc != 0 and "syntax error" in err


def test_a_check_is_filled_and_parses_too():
    _cmds, verify, _n = read_on_the_machine(["pct exec 101 -- sh -c 'true'"], [LIVE], INV)
    assert "<JELLYFIN_API_KEY>" not in verify[0]
    rc, err = _bash_n(verify[0])
    assert rc == 0, f"{err}\n\n{verify[0]}"


def test_the_arr_read_still_parses_and_is_not_escaped():
    """The *arr extract carries no single quote, so nothing should change for it
    — a fix that rewrote the working family would be a regression."""
    cmd = ("pct exec 103 -- sh -c 'curl -s -H \"X-Api-Key: <RADARR_API_KEY>\" "
           "\"http://127.0.0.1:7878/api/v3/movie\"'")
    cmds, _v, _n = read_on_the_machine([cmd], [], INV)
    rc, err = _bash_n(cmds[0])
    assert rc == 0, err
    assert "'\\''" not in cmds[0], "nothing to escape, so nothing escaped"


# ── the rule itself ─────────────────────────────────────────────────────────

def test_a_placeholder_outside_single_quotes_is_left_alone():
    cmd = 'pct exec 101 -- sh -c "curl -H \\"K: <JELLYFIN_API_KEY>\\" http://x"'
    assert _for_context("a'b", cmd, "<JELLYFIN_API_KEY>") == "a'b"


def test_a_placeholder_inside_single_quotes_is_escaped():
    cmd = "pct exec 101 -- sh -c 'curl <JELLYFIN_API_KEY>'"
    assert _for_context("a'b", cmd, "<JELLYFIN_API_KEY>") == "a'\\''b"


def test_a_value_with_no_quote_is_untouched_either_way():
    for cmd in ("sh -c 'x <P>'", 'sh -c "x <P>"'):
        assert _for_context("plain", cmd, "<P>") == "plain"


@pytest.mark.parametrize("value", ["a'b", "it's", "python3 -c 'import os'"])
def test_the_escaped_value_round_trips_through_a_real_shell(value):
    """Not an opinion about quoting: the shell is asked."""
    cmd = "printf '%s' '<P>'"
    filled = cmd.replace("<P>", _for_context(value, cmd, "<P>"))
    out = subprocess.run(["bash", "-c", filled], text=True, capture_output=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == value


# ── §17.1387 — and the compile gate must read what the shell delivers ────────

def test_the_compile_gate_accepts_the_engine_filled_command():
    """The second half of the same live failure. §17.1386's escaping made the
    command correct, and `payload_will_not_compile` then refused it:

        the `python -c` payload is not valid Python … unexpected EOF while
        parsing (line 1) -- '\\\\'

    It had scanned the RAW text, stopped at the first `'` of `'\\''`, and
    compiled a lone backslash. The engine refusing its own correct command is
    worse than the original bug, because the remedy it printed was wrong too."""
    from app.modules.supervised_runs import payload_will_not_compile
    cmds, _v, _n = read_on_the_machine([LIVE], [], INV)
    assert payload_will_not_compile(cmds) == []


def test_the_payload_extracted_is_what_the_shell_delivers():
    """Not 'it compiles' — the exact source, matching what a real shell hands
    over (asserted against `bash` in the round-trip test above)."""
    import ast
    from app.modules.supervised_runs import python_payloads
    cmds, _v, _n = read_on_the_machine([LIVE], [], INV)
    what, src = python_payloads(cmds[0])[0]
    assert src and src.startswith("import sqlite3")
    assert "\\" not in src, "the escape belongs to the outer word, not the payload"
    ast.parse(src)


@pytest.mark.parametrize("cmd,why", [
    ("""python3 -c 'import json; x = \\"a\\"'""", "an escaped quote inside a single-quoted payload"),
    ("""python3 -c 'import os; print('""", "an unterminated call"),
    ("""pct exec 103 -- sh -c 'python3 -c "import os; print("'""", "the same, one shell down"),
])
def test_a_real_broken_payload_is_still_refused(cmd, why):
    """The fix must not blunt the gate: §17.1257 exists because three drafts in
    a row shipped exactly these."""
    from app.modules.supervised_runs import payload_will_not_compile
    assert len(payload_will_not_compile([cmd])) == 1, why


def test_a_valid_plain_payload_is_still_accepted():
    from app.modules.supervised_runs import payload_will_not_compile
    assert payload_will_not_compile(["python3 -c 'import os; print(os.getcwd())'"]) == []


def test_an_unterminated_payload_is_not_this_gates_finding():
    """Checked, not assumed: `python3 -c 'import os` with no closing quote
    matches no `-c` payload at all — before this change either, since the
    pattern has always required the closing quote. The shell parse owns an
    unbalanced command, and a test that claimed this gate caught it would be
    asserting a guarantee nothing provides."""
    from app.modules.supervised_runs import payload_will_not_compile, python_payloads
    assert python_payloads("""python3 -c 'import os""") == []
    assert payload_will_not_compile(["""python3 -c 'import os"""]) == []
