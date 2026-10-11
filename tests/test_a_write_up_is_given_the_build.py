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


FW106 = pathlib.Path("tests/fixtures/fw_106_2026_10_11.txt").read_text()


def test_a_guests_firewall_is_read_off_its_own_file():
    """§17.1482 — T38 wrote "security groups … pending" with every guest's firewall on (fixture: VM 106's real file)."""
    got = ir.guest_firewall("virtio=BC:24:11:E8:9F:7A,bridge=vmbr0,firewall=1", FW106)
    assert got.startswith("on (policy_in DROP;") and "8211/udp from anywhere" in got and "group game" in got
    assert ir.guest_firewall("virtio=x,bridge=vmbr0", "cat: /etc/pve/firewall/100.fw: No such file or directory") == "off"
    assert ir.guest_firewall("x,firewall=1", "") == "on (no rules file)"


def test_tailscale_is_read_off_its_own_status():
    """§17.1482 — T38 wrote the subnet route "pending" while it was approved and the phone had joined."""
    import json
    js = json.dumps({"BackendState": "Running", "TailscaleIPs": ["100.84.253.90"],
                     "Self": {"PrimaryRoutes": ["192.168.1.0/24"]}, "Peer": {"k": {"HostName": "Adam's S24 Ultra"}}})
    assert ir.tailscale_summary(js) == ("Running, 100.84.253.90; serving subnet route 192.168.1.0/24 (approved); "
                                         "1 other device(s): Adam's S24 Ultra")
    assert ir.tailscale_summary("") == ""


def test_the_list_outranks_a_stale_fact():
    assert "THIS LIST IS NEWER AND WINS" in ir.inventory_block([{"kind": "host", "id": "host", "name": "pve",
                                                                  "status": "running", "address": "x"}])


UNITS = pathlib.Path("tests/fixtures/vm106_units_2026_10_11.txt").read_text()


def test_how_a_service_runs_is_read_off_the_machine():
    """§17.1486 — T38 wrote "The server runs in Docker" for VM 106 (fixture: its real running units)."""
    got = ir.app_services(UNITS)
    assert got == "palworld.service (PalWorld Dedicated Server)", got


def test_no_docker_is_said_only_where_the_services_were_read():
    seen = ir.inventory_block([{"kind": "vm", "id": "106", "name": "palworld-server", "status": "running",
                                "address": "192.168.1.44", "services": "palworld.service", "_inside": True}])
    blind = ir.inventory_block([{"kind": "vm", "id": "110", "name": "ai-vm", "status": "running", "address": "unread"}])
    assert "runs palworld.service · no Docker" in seen
    assert "no Docker" not in blind and "services not read (no way inside)" in blind
