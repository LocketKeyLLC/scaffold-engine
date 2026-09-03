"""§17.914 — the engine keeps a structured model of the operator's system.

The root cause under §17.906-913. Measured on the live session (613dd1df): the
engine asked for `qm config 106` **21 times**; the operator pasted the answer
**6 times**. The environment had nowhere to keep it — only free-text `profile`,
LLM-distilled prose `facts`, and scalar lists — so ground truth arrived, was
read for one turn, and was thrown away. Every gate in §17.906-913 compensates
for that absence; §17.907 in particular *instructs* the engine to ask, which is
why it asked twenty-one times.
"""
from __future__ import annotations

import pytest

from app.modules.assist_state import (
    find_redundant_discovery,
    merge_system_state,
    parse_system_state,
    render_system_state,
)

pytestmark = pytest.mark.asyncio

LIVE_1421 = """root@pve:~# qm config 106
boot: order=scsi0
cores: 4
ide2: local:iso/ubuntu-22.04.3-live-server-amd64.iso,media=cdrom,size=2083390K
memory: 8192
meta: creation-qemu=11.0.0,ctime=1788469244
name: palworld-server
net0: virtio=BC:24:11:6A:C7:B8,bridge=vmbr0
scsi0: local-lvm:vm-106-disk-0,size=100G
scsihw: virtio-scsi-pci"""


def test_parses_the_live_paste():
    st = parse_system_state(LIVE_1421)
    assert list(st) == ["106"]
    rec = st["106"]
    assert rec["kind"] == "vm"
    assert rec["attrs"]["boot"] == "order=scsi0"       # the value it kept guessing at
    assert rec["attrs"]["name"] == "palworld-server"
    assert "ubuntu-22.04.3-live-server-amd64.iso" in rec["devices"]["ide2"]
    assert rec["devices"]["scsi0"].startswith("local-lvm:vm-106-disk-0")


def test_container_config_is_recorded_as_a_container():
    st = parse_system_state("root@pve:~# pct config 104\nhostname: sonarr\nmemory: 1024")
    assert st["104"]["kind"] == "ct"


@pytest.mark.parametrize("text", [
    "root@pve:~# qm start 106",                 # not a config read
    "boot: order=scsi0\nmemory: 8192",          # no command echo — never guess
    "",
])
def test_never_guesses(text):
    assert parse_system_state(text) == {}


def test_newer_observation_wins_and_others_survive():
    a = {"106": {"kind": "vm", "attrs": {"boot": "order=ide2"}, "devices": {}, "source": "x"}}
    b = {"106": {"kind": "vm", "attrs": {"boot": "order=scsi0"}, "devices": {}, "source": "y"},
         "104": {"kind": "ct", "attrs": {}, "devices": {}, "source": "z"}}
    merged = merge_system_state(a, b)
    assert merged["106"]["attrs"]["boot"] == "order=scsi0"
    assert "104" in merged


def test_render_marks_it_as_ground_truth():
    block = render_system_state(parse_system_state(LIVE_1421))
    assert "CONFIRMED resource CONFIGURATION" in block
    assert "order=scsi0" in block
    assert "do NOT ask them to re-run" in block


def test_render_scopes_itself_to_configuration_only():
    """§17.917 REGRESSION — the first header said only "GROUND TRUTH … do NOT
    contradict it", and the model drew a conclusion the data never supported.
    Live (turn 1445): "Guide me" on ADD5 "Install Ubuntu Server 22.04 on VM 106"
    returned an entirely POST-INSTALL walkthrough — fix the boot order, detach
    the ISO, "wait for the login prompt" — because this block showed a 100G
    scsi0 disk and boot: order=scsi0. A disk existing is not an OS existing."""
    block = render_system_state(parse_system_state(LIVE_1421))
    assert "does NOT establish" in block
    assert "installed" in block and "running" in block
    assert "not an OS being installed on it" in block


def test_state_reaches_the_real_injection_path():
    """§17.913's lesson: a record the model cannot see is a record the engine
    does not have. The first cut rendered ONLY into render_environment_block —
    but with assist_umem_inject on, the fix path uses render_session_memory, so
    the fix path never saw it and went on asking."""
    from app.modules.assist_render import render_session_memory
    env = {"profile": "root@pve", "system_state": parse_system_state(LIVE_1421)}
    out = render_session_memory(env, [])
    assert "CONFIRMED resource CONFIGURATION" in out
    assert "order=scsi0" in out
    # §17.917 — the scope caveat must travel WITH it into the real prompt, or
    # the block regains the authority that produced the post-install guide.
    assert "does NOT establish" in out


def test_redundant_discovery_is_detected():
    st = parse_system_state(LIVE_1421)
    hits = find_redundant_discovery("```bash\nqm config 106\n```", st)
    assert hits and hits[0]["resource"] == "106"
    assert "boot=order=scsi0" in hits[0]["known"]


@pytest.mark.parametrize("draft,state_from_live", [
    ("```bash\nqm config 999\n```", True),        # unknown resource — must ask
    ("```bash\nqm config 106\n```", False),       # no state at all — must ask
    ("I would run qm config 106 next.", True),    # prose, not a prescription
])
def test_redundant_discovery_no_false_positives(draft, state_from_live):
    st = parse_system_state(LIVE_1421) if state_from_live else {}
    assert find_redundant_discovery(draft, st) == []


def test_look_gate_is_satisfied_by_known_state():
    """The two gates must agree. §17.907 exists to stop blind guessing, and a
    draft acting on values read from the operator's own output is the opposite
    of a guess — without this, the moment §17.914 got the model to USE the
    records, §17.907 flagged it for not re-reading them."""
    import inspect
    from app.modules import assist_guide
    src = inspect.getsource(assist_guide.generate_fix)
    assert "if look_ and _known_state:" in src
    assert "look_ = []" in src
