"""§17.1468 — a VM's cloud-init netplan is regenerated from its Proxmox settings at every boot.

Live (2026-10-11): ADD154 moved VM 106 (palworld-server) to DHCP by rewriting /etc/netplan/50-cloud-init.yaml
(fixture: the real in-guest script). The Proxmox host restarted at 02:13 and cloud-init rewrote the file from
`ipconfig0: ip=192.168.1.106/24,gw=192.168.1.1` (fixture: the real `qm config 106`) — the VM came back on .106
while the operator's router reservation and port forward pointed at .44.
"""
import asyncio
import pathlib

import pytest

from app.modules import address_watch as aw
from app.modules import rewrite_guard as rg

FIX = pathlib.Path("tests/fixtures")
SCRIPT = (FIX / "add154_in_vm_106_agent_2026_10_11.sh").read_text()
QM = (FIX / "qm_config_106_2026_10_11.txt").read_text()
CMDS = ["bash /tmp/in_vm_106_agent.sh start", "bash /tmp/in_vm_106_agent.sh last"]
FILES = [{"path": "/tmp/in_vm_106_agent.sh", "content": SCRIPT}]


@pytest.fixture
def machine(monkeypatch):
    import app.modules.service_truth as st

    async def probe(spec, cmd):
        return (True, QM) if cmd == "qm config 106" else (True, "")
    monkeypatch.setattr(st, "_probe", probe)


def test_the_live_block_is_refused(machine):
    out = asyncio.run(rg.cloud_init_netplan_writes(CMDS, object(), FILES))
    assert len(out) == 1 and rg.CLOUD_INIT_MARKER in out[0]["why"]
    assert "ipconfig0: ip=192.168.1.106/24,gw=192.168.1.1" in out[0]["why"]
    assert "qm set 106 --ipconfig0 ip=dhcp" in out[0]["why"]
    assert asyncio.run(rg.destructive_rewrites(CMDS, object(), FILES))[0]["why"] == out[0]["why"]


def test_a_backup_copy_and_a_vm_without_ipconfig0_are_not(monkeypatch):
    import app.modules.service_truth as st

    async def probe(spec, cmd):
        return True, QM.replace("ipconfig0:", "x-ipconfig0:")
    monkeypatch.setattr(st, "_probe", probe)
    assert asyncio.run(rg.cloud_init_netplan_writes(CMDS, object(), FILES)) == []
    backup_only = ["qm guest exec 106 -- cp /etc/netplan/50-cloud-init.yaml /etc/netplan/50-cloud-init.yaml.bak"]
    assert not rg._CLOUD_NETPLAN_WRITE_RE.search(backup_only[0])


def test_the_marker_redrafts():
    src = pathlib.Path("app/modules/supervised_runs.py").read_text()
    assert src.count(rg.CLOUD_INIT_MARKER) == 2


def test_a_move_already_carried_is_not_carried_again(monkeypatch):
    fact = "VM 106 (palworld-server) moved from 192.168.1.106 to 192.168.1.44 — measured on the machine after ADD154."

    class DB:
        async def execute(self, stmt, params=None):
            class R:
                def mappings(self_inner):
                    class M:
                        def first(self2):
                            return {"metadata": {"environment": {"facts": [fact]}}}
                    return M()
            return R()
    out = asyncio.run(aw.carry(DB(), "j", "ADD154", [{"id": "106", "old": "192.168.1.106", "new": "192.168.1.44"}],
                               {"106": True}))
    assert out["already_carried"] is True and out["added"] is None
