"""§17.1481 — a step that documents or validates the build is given the build, read off the machines.

Live (2026-10-11): T38, rewritten with every measured fact in reach, still documented Palworld as "deployment via
Docker" on LAN-only ports (VM 106 runs it natively at 192.168.1.44, UDP 8211 open; fixture: its real `ss -tulnH`),
claimed public reachability "confirmed from outside the LAN" (untested), and left out the host's address, Jellyfin,
the download client, Pi-hole and the AI VM: nothing in its prompt was the inventory as it stands.
"""
import pathlib

from app.modules import inventory_report as ir

SS = pathlib.Path("tests/fixtures/vm106_ss_tulnH_2026_10_11.txt").read_text()


def test_which_steps_get_it():
    assert ir.documents_or_validates({"title": "Document architecture and setup"})
    assert ir.documents_or_validates({"title": "Validate entire build"})
    assert not ir.documents_or_validates({"title": "Install Caddy in CT 120"})


def test_the_listeners_are_read_and_loopback_is_not():
    got = ir._listeners(SS)
    assert "8211/udp" in got and "22/tcp" in got
    assert "53/" not in got, "systemd-resolved on 127.0.0.53 is not a service the build offers"


def test_the_block_carries_the_rule_and_the_machines():
    block = ir.inventory_block([
        {"kind": "host", "id": "host", "name": "pve", "status": "running", "address": "192.168.1.156", "listens": "8006/tcp"},
        {"kind": "vm", "id": "106", "name": "palworld-server", "status": "running", "address": "192.168.1.44",
         "how": "DHCP", "devices": "", "listens": "8211/udp"},
        {"kind": "vm", "id": "110", "name": "ai-vm", "status": "running", "address": "unread", "how": "",
         "devices": "hostpci0=02:00.0,pcie=1", "listens": ""}])
    assert "Proxmox host (pve) · running · address 192.168.1.156" in block
    assert "VM 106 (palworld-server) · running · address 192.168.1.44 (DHCP) · listens on 8211/udp" in block
    assert "address unread" in block and '"not verified"' in block and "never describe planned work as done" in block


def test_it_is_wired_into_the_prompt_and_the_grounding():
    src = pathlib.Path("app/modules/execution_agent.py").read_text()
    assert "_inventory_text = await _ir.block_for(node_snapshot, _idb)" in src
    assert "_grounding_blocks.append(_inventory_text.strip())" in src
    assert 'upstream_text=(_inventory_text or "") + ' in src
