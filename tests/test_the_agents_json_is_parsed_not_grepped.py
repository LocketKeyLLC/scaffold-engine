r"""§17.1400 — `qm guest exec` answers JSON; a text tool reading it reads the wrapper.

Live, 2026-10-06. ADD137's draft did what §17.1399 asked — read VM 106's real host
key through the guest agent, pin it, ssh with `StrictHostKeyChecking=yes` — and
read the key with

    qm guest exec 106 -- cat /etc/ssh/ssh_host_ed25519_key.pub |
        sed -n 's/.*"out-data":"\([^"]*\)".*/\1/p'

The agent answers pretty-printed JSON (`"out-data" : "…\n"`, spaces around the
colon), as the step's own previous run printed (`"exitcode" : 0`). The pattern
matches nothing and the step stops. Caught reading the frame, before approval.
The run before did the same with `| grep -o '[0-9]*'` and printed "0 1 4 lines".
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from app.modules import supervised_runs as sr
from app.modules.runbook_templates import TEMPLATES
from app.modules.supervised_runs import the_agents_json_read_as_text as GATE

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add137_agent_json_read_by_sed_2026_10_06.json").read_text())
#: what the agent really prints (this host, 2026-10-06, ADD137's run)
AGENT = '{\n   "exitcode" : 0,\n   "exited" : 1,\n   "out-data" : "ssh-ed25519 KEYTEXT root@palworld-server\\n"\n}\n'


def test_the_premise_the_live_sed_reads_nothing_from_the_real_answer():
    """Run the draft's own pattern on the agent's real output shape."""
    import re
    assert re.search(r'.*"out-data":"([^"]*)".*', AGENT) is None
    assert json.loads(AGENT)["out-data"].startswith("ssh-ed25519 ")


def test_the_live_draft_is_refused():
    out = GATE(LIVE["commands"], LIVE["files"])
    assert len(out) == 1
    assert "out-data" in out[0]["command"]
    assert "json.load(sys.stdin)" in out[0]["why"]


@pytest.mark.parametrize("line", [
    "qm guest exec 106 -- sh -c 'wc -l < /x' | grep -o '[0-9]*'",     # the run before
    "qm guest exec 106 -- hostname | awk '{print $3}'",
    "X=$(qm guest exec 106 -- cat /etc/hostname | cut -d'\"' -f4)",
])
def test_every_text_tool_on_the_answer(line):
    assert GATE([line])


@pytest.mark.parametrize("line", [
    "qm guest exec 106 -- cat /x | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"out-data\"])'",
    "qm guest exec 106 -- cat /x | jq -r '.\"out-data\"'",
    "qm guest exec 106 -- sh -c 'grep -c x /etc/y | tail -1'",          # the pipe is INSIDE the guest command
    "qm guest exec 106 -- sh -c 'wc -l < /home/u/.ssh/authorized_keys'",
    "qm guest exec 106 -- true || echo failed",                           # || is not a pipe
    "echo k | qm guest exec 106 --pass-stdin 1 -- sh -c 'cat >> /x'",    # input, not output
])
def test_what_it_must_not_claim(line):
    assert GATE([line]) == [], line


def test_the_engines_own_template_passes():
    for t in TEMPLATES:
        for content in (getattr(t, "files", None) or {}).values():
            assert GATE([], [{"path": "x", "content": content}]) == [], t.name


def test_the_1399_remedy_no_longer_teaches_a_sed():
    """The §17.1399 refusal said "take `out-data`" and the drafter took it with
    sed. A remedy is an instruction; it now shows the parse."""
    why = sr.a_host_key_forgotten_not_pinned(["ssh-keygen -R 192.168.1.106"])[0]["why"]
    assert "json.load(sys.stdin)" in why and "never sed it" in why
    # and the remedy's own command passes this gate
    import re
    cmd = re.search(r"`(qm guest exec <id> -- cat [^`]+)`", why).group(1).replace("<id>", "106")
    assert GATE([cmd]) == []


def test_wired_registered_and_the_drafters_gap():
    assert "the_agents_json_read_as_text(cmds, shape_files)" in inspect.getsource(sr.frame_run)
    refused = GATE(LIVE["commands"], LIVE["files"])
    assert sr.shape_retry_note({"kind": "run", "refused": refused})
    assert sr._WHOSE_GAP["answers JSON, and this hands that JSON to a text tool"] == "drafter"
