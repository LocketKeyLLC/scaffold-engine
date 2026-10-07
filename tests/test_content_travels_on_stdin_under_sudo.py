"""§17.1405 — a redirect is the calling shell's, not sudo's; and JSON is not shell quoting.

Live, 2026-10-06. ADD122's draft (the first to get the structure right: a host
script, `pct push`, whole-file read with the template as fallback) wrote the
Palworld settings over ssh as aedefruscio with

    sudo systemctl stop palworld.service && printf '%s' ${quoted} > ${PALWORLD_CONFIG} && sudo systemctl start …

where `quoted = JSON.stringify(content)`. Measured: the file is `steam:steam 644`.
1. sudo covers systemctl; the `>` is aedefruscio's own shell — Permission denied.
2. JSON writes newlines as the two characters `\\n`; `printf '%s'` prints them
   literally, so the multi-line ini becomes one corrupt line.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from app.modules import service_truth as st
from app.modules import supervised_runs as sr
from app.modules.supervised_runs import json_used_as_shell_quoting as JSONQ

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add122_sudo_redirect_and_json_quoting_2026_10_06.json").read_text())
LIVE_INI = "/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
PAL = st.ServiceTruth(guest="106", vm=True, name="palworld", configs=(LIVE_INI,),
                      config_stat={LIVE_INI: ("steam:steam", "644")})


# ── the redirect sudo does not cover ─────────────────────────────────────────

def test_the_live_draft_is_refused_for_the_redirect():
    out = st.a_redirect_sudo_does_not_cover(LIVE["commands"], LIVE["files"], [PAL])
    assert len(out) == 1
    why = out[0]["why"]
    assert "is done by the calling shell, not by sudo" in why
    assert "`steam:steam 644`" in why and f"sudo tee {LIVE_INI}" in why


def test_the_constant_is_resolved():
    assert st.with_constants("const CFG = '/x/y.ini';\nprintf a > ${CFG}") == "const CFG = '/x/y.ini';\nprintf a > /x/y.ini"


@pytest.mark.parametrize("line", [
    f"printf '%s' \"$C\" | sudo tee {LIVE_INI} >/dev/null",
    f"sudo -u steam tee {LIVE_INI} < /tmp/new",
    f"sudo sh -c 'cat /tmp/new > {LIVE_INI}'",
    f"sudo systemctl stop palworld && cat /tmp/x | sudo tee {LIVE_INI}",
])
def test_a_write_under_sudo_passes(line):
    assert st.a_redirect_sudo_does_not_cover([line], [], [PAL]) == []


def test_no_sudo_at_all_is_not_this_gate():
    """A root shell (pct exec / qm guest exec) needs no sudo; that is not this mistake."""
    assert st.a_redirect_sudo_does_not_cover([f"printf x > {LIVE_INI}"], [], [PAL]) == []


def test_a_root_owned_file_is_left_alone():
    root = st.ServiceTruth(guest="103", name="radarr", config_stat={"/etc/x.conf": ("root:root", "644")})
    assert st.a_redirect_sudo_does_not_cover(["sudo systemctl stop x && printf y > /etc/x.conf"], [], [root]) == []


def test_unmeasured_is_silent():
    assert st.a_redirect_sudo_does_not_cover(LIVE["commands"], LIVE["files"], []) == []


# ── JSON is not shell quoting ────────────────────────────────────────────────

def test_the_live_draft_is_refused_for_json_quoting():
    out = JSONQ(LIVE["commands"], LIVE["files"])
    assert len(out) == 1
    assert "JSON is not shell quoting" in out[0]["why"] and "{ input: content }" in out[0]["why"]


def test_the_premise_json_newlines_are_not_newlines():
    assert json.dumps("[S]\nK=1") == '"[S]\\nK=1"'       # two characters, not a line break


@pytest.mark.parametrize("src", [
    "const body = JSON.stringify(settings);\nres.json(JSON.parse(body));",                       # not a shell
    "execFile('ssh', [t, 'sudo tee /x >/dev/null'], { input: content });",
    "fetch(url, { method: 'PUT', body: JSON.stringify(obj) });",
])
def test_what_it_must_not_claim(src):
    assert JSONQ([], [{"path": "/x.js", "content": src}]) == []


def test_inline_stringify_in_an_ssh_command_is_caught():
    src = "execSync(`ssh u@h ${JSON.stringify(cmd)}`);"
    assert JSONQ([], [{"path": "/x.js", "content": src}])


# ── wired ────────────────────────────────────────────────────────────────────

def test_wired_registered_and_the_drafters_gap():
    src = inspect.getsource(sr.frame_run)
    assert "json_used_as_shell_quoting(cmds, shape_files)" in src
    assert "_st.a_redirect_sudo_does_not_cover(cmds, files, services)" in src
    for refused in (JSONQ(LIVE["commands"], LIVE["files"]),
                    st.a_redirect_sudo_does_not_cover(LIVE["commands"], LIVE["files"], [PAL])):
        assert sr.shape_retry_note({"kind": "run", "refused": refused})
    assert sr._WHOSE_GAP["and JSON is not shell quoting"] == "drafter"
    assert sr._WHOSE_GAP["is done by the calling shell, not by sudo"] == "drafter"
