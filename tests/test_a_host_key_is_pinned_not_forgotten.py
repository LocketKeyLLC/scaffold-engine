"""§17.1399 — an ssh FROM a guest is not this host reaching in, and a forgotten host key is pinned.

Live, 2026-10-06. ADD137's ssh check failed with `REMOTE HOST IDENTIFICATION HAS
CHANGED` (§17.1393 judged it), the failure came back to the drafter after a reset
(`prior_attempt_recovered … chars=5460`, §17.1260), and the redraft acted on it.
Two things were then wrong, one in the engine and one in the draft:

1. §17.1288g refused it: "nothing has put this host's key on guest 111". The ssh
   was `pct exec 111 -- ssh … aedefruscio@192.168.1.106` — the PANEL reaching VM
   106 with the panel's own key, which this same block installs on 106. Not this
   host reaching into 111.
2. The draft ran `ssh-keygen -R 192.168.1.106` then `StrictHostKeyChecking=accept-new`:
   delete the warning, trust whatever answers next. VM 106 is a guest on this
   host; its agent is a channel that is not the network being doubted, and the
   real key can be read through it and pinned.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from app.modules import runbook_preconditions as rp
from app.modules import supervised_runs as sr
from app.modules.supervised_runs import a_host_key_forgotten_not_pinned as GATE

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add137_forgets_host_key_2026_10_06.json").read_text())
INV = {"cts": {"111": "running"}, "vms": {"106": "running"},
       "names": {"106": "palworld-server", "111": "control-panel"}}
NODE = {"title": LIVE["title"], "description": "The panel (CT 111) must be able to ssh to VM 106."}


# ── 1. an ssh from a guest ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_live_draft_is_not_refused_for_a_key_on_111():
    out = await rp.unmet(LIVE["commands"], None, files=LIVE["files"], node=NODE, inventory=INV, plan=[])
    assert not [r for r in out if "nothing has put this host's key on guest" in r["why"]], out


@pytest.mark.parametrize("line", [
    "pct exec 111 -- ssh -o BatchMode=yes aedefruscio@192.168.1.106 true",
    "qm guest exec 110 -- sh -c 'ssh -o BatchMode=yes u@192.168.1.106 true'",
])
def test_an_ssh_inside_a_guest_leaves_from_it(line):
    assert rp.ssh_runs_inside_a_guest(line)


@pytest.mark.parametrize("line", [
    "ssh -o BatchMode=yes aedefruscio@192.168.1.106 true",          # this host reaching out
    "pct exec 111 -- true; ssh u@192.168.1.106 true",                 # the ssh is after the exec ended
    "pct exec 111 -- true && ssh u@192.168.1.106 true",
])
def test_an_ssh_from_this_host_is_still_judged(line):
    assert not rp.ssh_runs_inside_a_guest(line)


@pytest.mark.asyncio
async def test_a_host_ssh_into_a_keyless_guest_is_still_refused():
    """Vacuity guard: §17.1288g still bites on the shape it was built for."""
    out = await rp.unmet(["ssh -o BatchMode=yes aedefruscio@192.168.1.106 true"], None,
                         node={"title": "Configure VM 106", "description": ""}, inventory=INV, plan=[])
    assert [r for r in out if "nothing has put this host's key on guest 106" in r["why"]]


# ── 2. a host key forgotten, nothing pinned ──────────────────────────────────

def test_the_live_draft_is_refused():
    out = GATE(LIVE["commands"], LIVE["files"])
    assert len(out) == 1
    assert "ssh-keygen -R 192.168.1.106" in out[0]["command"]
    why = out[0]["why"]
    assert "man-in-the-middle" in why
    assert "qm guest exec <id> -- cat /etc/ssh/ssh_host_ed25519_key.pub" in why
    assert "out-data" in why, "the agent answers JSON (§17.1319)"
    assert "StrictHostKeyChecking=yes" in why


@pytest.mark.parametrize("line", [
    "ssh-keygen -R 192.168.1.106",
    "ssh-keygen -f /root/.ssh/known_hosts -R 192.168.1.106",
    "sed -i '/192.168.1.106/d' /root/.ssh/known_hosts",
    "rm -f /root/.ssh/known_hosts",
    ": > /root/.ssh/known_hosts",
    "grep -v 192.168.1.106 /root/.ssh/known_hosts > /tmp/kh",
])
def test_every_way_a_host_key_is_forgotten(line):
    assert GATE([line])


def test_reading_the_guests_own_key_and_pinning_it_passes():
    pinned = [
        "pct exec 111 -- ssh-keygen -R 192.168.1.106",
        "HK=$(qm guest exec 106 -- cat /etc/ssh/ssh_host_ed25519_key.pub | python3 -c "
        "'import json,sys; print(json.load(sys.stdin)[\"out-data\"].strip())') && "
        "pct exec 111 -- sh -c \"echo '192.168.1.106 $HK' >> /root/.ssh/known_hosts\"",
    ]
    assert GATE(pinned) == []


@pytest.mark.parametrize("line", [
    "printf '%s\\n' \"$LINE\" >>/root/.ssh/known_hosts",          # an APPEND forgets nothing
    "cat /root/.ssh/known_hosts",
    "ssh -o StrictHostKeyChecking=accept-new u@h true",           # first contact, nothing forgotten
    "# ssh-keygen -R 192.168.1.106 was the old way",
])
def test_what_it_must_not_claim(line):
    assert GATE([line]) == []


# ── wired ────────────────────────────────────────────────────────────────────

def test_wired_registered_and_the_drafters_gap():
    assert "a_host_key_forgotten_not_pinned(cmds, shape_files)" in inspect.getsource(sr.frame_run)
    refused = GATE(LIVE["commands"], LIVE["files"])
    assert sr.shape_retry_note({"kind": "run", "refused": refused})
    assert sr._WHOSE_GAP["forgets a host key and pins nothing"] == "drafter"


def test_the_fixture_is_the_live_draft_and_carries_no_key():
    blob = json.dumps(LIVE)
    assert "ssh-keygen -R 192.168.1.106" in blob
    assert "AAAA" not in blob and "PRIVATE" not in blob
