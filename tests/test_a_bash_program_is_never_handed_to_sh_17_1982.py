"""§17.1982 — a program handed to `sh` that uses bash-only syntax runs with `bash`, or is refused with the remedy.

Live (job 0fb4ea7f CL3, 2026-11-21, frame 6de9e152c92bd675): the drafted block ran
`qm guest exec 106 --timeout 30 -- sh -c '… cmp -s <(sed -e "\\$a\\\\" "$A") "$B" …'`. The guest's `sh` is dash:
`Syntax error: "(" unexpected`, the whole program dies unparsed, and the guarded `rm` could never run. No gate
looked inside the `sh -c` word.
"""
from __future__ import annotations

import inspect
import os
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from app.modules import supervised_runs as sr
from app.modules.shell_ast import bash_only_constructs, sh_programs
from app.modules.supervised_runs import _SHAPE_REFUSALS, _WHOSE_GAP, bash_programs_handed_to_sh, whose_gap

# the live shape, with a made-up path
_PROG = ('A=/srv/game/Settings.ini; B=/srv/game/Settings.ini.bak-1; if cmp -s "$A" "$B"; then echo IDENTICAL; '
         'elif cmp -s <(sed -e "\\$a\\\\" "$A") "$B" || cmp -s "$A" <(sed -e "\\$a\\\\" "$B"); then echo ONLY_NEWLINE; '
         'else echo DIFFERS; fi')
LIVE = f"qm guest exec 106 --timeout 30 -- sh -c '{_PROG}'"
POSIX = "qm guest exec 106 --timeout 30 -- sh -c 'ls -la /srv/game; wc -l /srv/game/Settings.ini'"


# ── the live shape: repaired, and the repair is ONLY the shell word ─────────

def test_the_live_command_runs_with_bash_and_nothing_else_changes():
    cmds, verify, files, notes, refused = bash_programs_handed_to_sh([POSIX, LIVE], [], [])
    assert cmds[0] == POSIX, "a POSIX program is left alone"
    assert cmds[1] == LIVE.replace("-- sh -c '", "-- bash -c '", 1)
    assert refused == [] and files == [] and verify == []
    assert len(notes) == 1 and "§17.1982" in notes[0]["why"] and "process substitution" in notes[0]["why"]


def _shim(command: str) -> subprocess.CompletedProcess:
    """Run the WHOLE drafted command line on this box: `qm guest exec N … --` becomes "run the rest here"."""
    script = 'qm() { while [ "$1" != "--" ]; do shift; done; shift; "$@"; }\n' + command
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)


@pytest.mark.skipif(not shutil.which("dash") or os.path.realpath(shutil.which("sh") or "") != os.path.realpath(
    shutil.which("dash") or "x"), reason="needs dash as sh (as on Debian/Ubuntu)")
def test_bash_level_the_live_program_dies_under_dash_and_the_repair_runs(tmp_path):
    a, b = tmp_path / "Settings.ini", tmp_path / "Settings.ini.bak-1"
    a.write_text("x=1\ny=2")                       # the live config: no trailing newline
    b.write_text("x=1\ny=2\n")                     # the backup: the same, with one
    live = LIVE.replace("/srv/game", str(tmp_path))
    before = _shim(live)
    assert before.returncode != 0 and "Syntax error" in before.stderr, before
    assert before.stdout == "", "dash parses the whole program first: not a line ran"
    fixed, *_ = bash_programs_handed_to_sh([live], [], [])
    after = _shim(fixed[0])
    assert after.returncode == 0 and after.stdout.strip() == "ONLY_NEWLINE", after


# ── each construct, read off the AST ────────────────────────────────────────

@pytest.mark.parametrize("prog", [
    "cmp <(a) b", "tee >(cat)", "[[ -f x ]] && echo y", "echo ${v//a/b}", "echo ${v/a/b}", "echo ${v:1:2}",
    "echo ${v^^}", "echo ${v,,}", "a=(1 2)", 'echo "${a[@]}"', "function f { :; }", "ls &> /dev/null",
    'cat <<< "$x"', "echo $'a\\n'", "echo {1..5}", "source /etc/os-release", "echo ${!v}"])
def test_every_bash_only_construct_is_seen(prog):
    assert bash_only_constructs(prog), prog


@pytest.mark.parametrize("prog", [
    "echo ${v:-d} ${#v} ${v%x} ${v#y} ${v:=d} ${v:+s}", "x=$((1+2))", ". /etc/os-release", "f() { :; }",
    "ls >f 2>&1", "[ -f x ] && echo y", "python3 -c '[[1]]'", "bash -c '[[ -f x ]]'", "echo '<(x)'",
    "case $x in a) ;; esac", "cat <<'EOF'\n[[ x ]]\nEOF"])
def test_posix_programs_are_not_hits(prog):
    assert bash_only_constructs(prog) == [], prog


# ── where the shell sits ────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,want", [
    ("pct exec 101 -- sh -c 'echo ${a[@]}'", "pct exec 101 -- bash -c 'echo ${a[@]}'"),
    ("sudo sh -ec '[[ -d /x ]]'", "sudo bash -ec '[[ -d /x ]]'"),
    ("pct exec 1 -- /bin/sh -c 'cat <<< hi'", "pct exec 1 -- /bin/bash -c 'cat <<< hi'"),
    ("sh -c 'ls &> /dev/null' && echo ok", "bash -c 'ls &> /dev/null' && echo ok"),
    ("pct exec 1 -- sh -s <<'EOF'\n[[ -f /x ]] && echo y\nEOF", "pct exec 1 -- bash -s <<'EOF'\n[[ -f /x ]] && echo y\nEOF"),
])
def test_the_shell_word_is_swapped_wherever_it_runs(cmd, want):
    cmds, _v, _f, notes, refused = bash_programs_handed_to_sh([cmd], [], [])
    assert cmds == [want] and notes and not refused


@pytest.mark.parametrize("cmd", [
    "grep sh -c file", "echo sh -c '[[ x ]]'", "bash -c 'cmp <(a) b'",
    "pct exec 105 -- sh -c 'umask 077; cat > /x' <<< \"$P\"",       # the `<<<` is the HOST bash's, not the program's
    "pct exec 1 -- sh -c 'python3 -c \"[[1]]\"; bash -c \"[[ -f x ]]\"'"])
def test_correct_misses_are_left_alone(cmd):
    assert bash_programs_handed_to_sh([cmd], [cmd], []) == ([cmd], [cmd], [], [], [])


def test_a_written_file_run_by_sh_is_run_by_bash():
    files = [{"path": "/tmp/check.sh", "content": "source /etc/os-release\necho $ID\n"}]
    cmds, _v, out_files, notes, refused = bash_programs_handed_to_sh(["sh /tmp/check.sh"], [], files)
    assert cmds == ["bash /tmp/check.sh"] and out_files == files and notes and not refused


def test_a_written_files_own_sh_c_is_repaired_in_the_file():
    files = [{"path": "/tmp/s.sh", "content": "#!/usr/bin/env bash\npct exec 101 -- sh -c '[[ -f /x ]] && echo y'\n"}]
    _c, _v, out_files, notes, refused = bash_programs_handed_to_sh(["bash /tmp/s.sh"], [], files)
    assert out_files[0]["content"] == "#!/usr/bin/env bash\npct exec 101 -- bash -c '[[ -f /x ]] && echo y'\n"
    assert notes and not refused


# ── where the engine cannot say bash is there: refused, for a redraft ──────

@pytest.mark.parametrize("cmd", [
    "docker exec pg sh -c '[[ -f /x ]]'",                        # an image's sh may be busybox, with no bash
    "ssh u@h 'sh -c \"cmp <(a) b\"'",                           # inside a quoted remote command
])
def test_refused_with_the_remedy_where_bash_is_not_known(cmd):
    cmds, _v, _f, notes, refused = bash_programs_handed_to_sh([cmd], [], [])
    assert cmds == [cmd] and not notes and len(refused) == 1
    why = refused[0]["why"]
    assert "bash -c" in why and whose_gap(why) == "drafter"
    assert sr.shape_retry_note({"refused": refused, "commands": [cmd]}), "a redraft is asked for"


def test_registered_for_redraft():
    assert "uses bash-only syntax" in _SHAPE_REFUSALS and _WHOSE_GAP["uses bash-only syntax"] == "drafter"


# ── wired into the frame ────────────────────────────────────────────────────

def _spec():
    s = MagicMock(); s.name = "pve-runner"; s.headers = {"X-Runner-Token": "tok"}; return s


def test_the_frame_offers_the_repaired_command_and_says_so():
    runbook = (f"## Run this\n```bash\n{LIVE}\n```\n\n## Verify\n```bash\n"
               "qm guest exec 106 --timeout 30 -- ls -la /srv/game/\n```\n")
    f = sr.frame_run({"node_key": "CL3", "title": "remove the settings backup on VM 106"}, runbook, _spec(),
                     {"allow": ["qm guest exec"], "sudo": False})
    assert f["commands"] == [LIVE.replace("-- sh -c '", "-- bash -c '", 1)]
    assert any("§17.1982" in n for n in f["engine_fixed"])
    assert not [r for r in f["refused"] if "bash-only" in str(r.get("why"))]


def test_frame_run_calls_it():
    assert "bash_programs_handed_to_sh(" in inspect.getsource(sr.frame_run)
