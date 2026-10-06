"""§17.1395 — a network config never names an interface the machine does not have.

Live, 2026-10-06. ADD91: "Set vmbr0 bridge-ports to enp5s0f3 ... replacing the
stale nic3 reference ... applied via ifreload -a". An earlier draft of it wrote

    sed -i 's/bridge-ports .*/bridge-ports enp5s0f3/' /etc/network/interfaces

and was stopped only because it produced instructions instead of running. Before
the next attempt the operator asked for a measurement first, through the engine's
read-only box, and it settled the step: there IS no `enp5s0f3`. Proxmox pins NIC
names (`/usr/local/lib/systemd/network/50-pmx-nic3.link` binds MAC …:f0:7b to
`nic3`); `nic3` is UP with carrier and is vmbr0's one port, carrying
192.168.1.156, unchanged across that morning's reboot. The step had the stale
name backwards. Run, it leaves vmbr0 with no port and the host off its own
network until someone reaches the console.

No gate looked: `unmet` read the guests, their disks and the ISOs, never the
host's own interfaces.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from app.modules import runbook_preconditions as rp
from app.modules import supervised_runs as sr
from app.modules.runbook_preconditions import (a_bridge_port_the_machine_does_not_have as GATE,
                                               parse_bridge_ports, parse_ip_link)

FIX = pathlib.Path(__file__).parent / "fixtures"
HOST = json.loads((FIX / "pve_links_2026_10_06.json").read_text())
LINKS = parse_ip_link(HOST["ip_br_link"])
BRIDGES = parse_bridge_ports(HOST["bridge_link"])
LIVE_LINE = "sed -i 's/bridge-ports .*/bridge-ports enp5s0f3/' /etc/network/interfaces && ifreload -a"


# ── what the host says ───────────────────────────────────────────────────────

def test_the_measured_links_parse():
    assert LINKS["nic3"] is True, "nic3 has carrier"
    assert LINKS["nic0"] is False
    assert "veth111i0" in LINKS, "the @if2 suffix is not part of the name"
    assert "enp5s0f3" not in LINKS


def test_the_bridge_port_is_read_without_the_guests_taps():
    assert BRIDGES == {"vmbr0": ["nic3"]}


# ── the live defect ──────────────────────────────────────────────────────────

def test_the_live_add91_draft_is_refused():
    out = GATE([LIVE_LINE], LINKS, BRIDGES)
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "`enp5s0f3` is not an interface on this host" in why
    assert "`nic3`" in why, "the remedy names the interface that carries the link"
    assert "vmbr0's port today is `nic3`" in why, "and that the bridge already uses it"
    assert "goal is met" in why


def test_the_sed_pattern_side_is_not_read_as_an_interface():
    """`s/bridge-ports .*/…` — the `.*` is the pattern, not a port name."""
    out = GATE([LIVE_LINE], LINKS, BRIDGES)
    assert all(".*" not in r["why"].split("is not an interface")[0] for r in out)


@pytest.mark.parametrize("text", [
    "auto vmbr0\niface vmbr0 inet static\n    bridge-ports enp5s0f3\n",          # a written file
    "printf 'bridge-ports enp5s0f3\\n' >> /etc/network/interfaces",
    "sed -i 's/^\\s*bridge-ports nic3$/    bridge-ports eth0/' /etc/network/interfaces",
])
def test_every_way_the_name_reaches_the_config(text):
    assert GATE([text], LINKS, BRIDGES)


# ── what it must not claim ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "sed -i 's/bridge-ports .*/bridge-ports nic3/' /etc/network/interfaces",    # it exists
    "    bridge-ports none",                                                     # a bridge with no port, on purpose
    "auto bond0\niface bond0 inet manual\n    bond-slaves nic0 nic1\nauto vmbr1\niface vmbr1 inet manual\n    bridge-ports bond0\n",   # defined in the block
    "ip link add name dummy0 type dummy && printf 'bridge-ports dummy0\\n'",    # created in the block
    "# bridge-ports enp5s0f3 was the old plan's guess",                         # a comment
    "grep -n bridge-ports /etc/network/interfaces",                              # a read, names nothing
])
def test_what_it_must_not_claim(text):
    assert GATE([text], LINKS, BRIDGES) == []


def test_blindness_refuses_nothing():
    assert GATE([LIVE_LINE], None) == []
    assert GATE([LIVE_LINE], {}) == []


def test_one_refusal_per_name():
    assert len(GATE([LIVE_LINE, LIVE_LINE, "bridge-ports enp5s0f3"], LINKS, BRIDGES)) == 1


# ── wired where it has to be ─────────────────────────────────────────────────

def test_the_inventory_reads_the_links():
    src = inspect.getsource(rp.read_inventory)
    assert '"ip -br link"' in src and '"bridge link"' in src


def test_unmet_judges_it_before_the_guest_only_early_return():
    """A host network step names no guest; judged after that return, it is never judged."""
    src = inspect.getsource(rp.unmet)
    gate = src.index("a_bridge_port_the_machine_does_not_have(")
    early = src.index("if (not guests and not subjects) or not inv:")
    assert gate < early


@pytest.mark.asyncio
async def test_unmet_refuses_the_live_draft_end_to_end():
    inv = {"cts": {}, "vms": {}, "names": {}, "links": LINKS, "bridges": BRIDGES}
    out = await rp.unmet([LIVE_LINE], None, inventory=inv,
                         node={"title": "Set vmbr0 bridge-ports to enp5s0f3", "description": ""})
    assert any("is not an interface on this host" in r["why"] for r in out)


def test_the_refusal_drives_a_redraft_and_is_the_machines_gap():
    refused = GATE([LIVE_LINE], LINKS, BRIDGES)
    assert sr.shape_retry_note({"kind": "run", "refused": refused})
    assert sr._WHOSE_GAP["is not an interface on this host"] == "machine"
