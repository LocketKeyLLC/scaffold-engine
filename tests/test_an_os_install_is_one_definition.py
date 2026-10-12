"""§17.1496 — an OS install has one definition; a guest's address is its IPv4 entry.

§17.1297 fixed the OS-install regex in runbook_templates ("install" and the OS within one
short span) after ADD65 "Verify QEMU Guest Agent responds on VM 106" was templated as an OS
install. Two siblings kept the greedy `\\binstall\\b.*\\b(ubuntu|…|os)\\b`:

- machine_truth, which after a DONE OS install REOPENS every earlier in-guest step on that
  guest and writes an "ENGINE MEASURED: reinstalled the OS" fact -- so "Install
  qemu-guest-agent in the Ubuntu VM 106" recorded done reset the guest's finished work;
- runbook_preconditions, which waives the empty-disk refusal for the install step -- so
  "Install the OS packages Radarr needs on VM 106" was let through to ssh into a VM with
  no OS.

Also here: `ip neigh` lists a `fe80::…` entry with the same lladdr; taken first, it became
the guest's "address" every ssh target was then refused against.
"""
from __future__ import annotations

import pathlib
import re
from datetime import datetime, timezone

from app.modules import machine_truth as mt
from app.modules import runbook_inputs as ri
from app.modules import runbook_preconditions as rp
from app.modules import runbook_templates as rt


def test_the_three_modules_share_the_one_regex():
    assert rt._INSTALL_OS_RE is ri.INSTALL_OS_RE and mt._INSTALL_OS_RE is ri.INSTALL_OS_RE
    src = pathlib.Path(rp.__file__).read_text(encoding="utf-8")
    assert "INSTALL_OS_RE.search(subject)" in src
    for mod in (mt, rp, rt):
        s = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert not re.search(r're\.(?:search|compile)\(r"\\binstall\\b\.\*', s), f"{mod.__name__} keeps a greedy copy"


def test_an_install_inside_the_os_is_not_an_install_of_the_os():
    for title in ("Install qemu-guest-agent in the Ubuntu VM 106",
                  "Install qbittorrent-nox from the Debian repositories in container 105",
                  "Verify QEMU Guest Agent responds on VM 106 (Ubuntu)"):
        assert not ri.INSTALL_OS_RE.search(title), title
    for title in ("Install Ubuntu 22.04 on VM 106 unattended (cloud image + cloud-init)",
                  "Reinstall the OS on VM 106", "Install a fresh Debian 12 on VM 110",
                  "install an operating system", "Install Ubuntu Server 22.04 on VM 106"):
        assert ri.INSTALL_OS_RE.search(title), title


def _at(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_a_package_install_recorded_done_reopens_nothing():
    plan = [
        {"node_key": "T23", "title": "Install the palworld-server service in VM 106", "status": "done",
         "completed_at": _at("2026-10-03T05:00:00")},
        {"node_key": "T24", "title": "Configure the palworld-server service in VM 106", "status": "done",
         "completed_at": _at("2026-10-03T05:10:00")},
        {"node_key": "ADD82", "title": "Install qemu-guest-agent in the Ubuntu VM 106", "status": "done",
         "completed_at": _at("2026-10-03T06:12:06")},
    ]
    assert mt.in_guest_work_voided_by_reinstall(plan, "106", "palworld-server") == []
    reinstall = dict(plan[2], title="Reinstall Ubuntu 22.04 on VM 106 from the cloud image")
    voided = mt.in_guest_work_voided_by_reinstall(plan[:2] + [reinstall], "106", "palworld-server")
    assert sorted(v["node_key"] for v in voided) == ["T23", "T24"], "a real reinstall still voids the work"


QM_CONFIG_110 = "agent: 1\nname: ai-vm\nnet0: virtio=BC:24:11:B4:AF:15,bridge=vmbr0\n"
NEIGH_V6_FIRST = """fe80::be24:11ff:feb4:af15 dev vmbr0 lladdr bc:24:11:b4:af:15 router STALE
192.168.1.129 dev vmbr0 lladdr bc:24:11:b4:af:15 STALE
192.168.1.1 dev vmbr0 lladdr 4c:ab:f8:d1:54:91 DELAY
"""


def test_the_address_is_the_ipv4_entry_wherever_ip_neigh_lists_it():
    t = mt.truth_from_texts("110", inventory={"cts": {}, "vms": {"110": {"status": "running"}}, "names": {"110": "ai-vm"}},
                            qm_config=QM_CONFIG_110, neigh=NEIGH_V6_FIRST)
    assert t.mac == "bc:24:11:b4:af:15"
    assert t.address == "192.168.1.129"
    t6 = mt.truth_from_texts("110", inventory={"cts": {}, "vms": {"110": {"status": "running"}}, "names": {"110": "ai-vm"}},
                             qm_config=QM_CONFIG_110, neigh=NEIGH_V6_FIRST.split("\n", 1)[0])
    assert t6.address == "fe80::be24:11ff:feb4:af15", "with only a v6 entry, that is what was measured"
