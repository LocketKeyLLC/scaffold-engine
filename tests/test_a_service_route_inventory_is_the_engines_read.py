r"""§17.1572 — "inventory the panel's endpoints" is the engine's own read of the measured service's code.

Live (job c2792d4b, T1 "Inventory panel endpoints"): the model drafted 24 exploratory `find / -maxdepth 5 … |
xargs grep` commands with no check — refused, `suggested: myself`. The engine knows the service, its guest and
its /opt/<name>* directories; a model cannot read remote files and need not guess where they are.
"""
from __future__ import annotations

import subprocess

from app.modules.supervised_runs import rewrite_inventory_service_routes, rewrite_disable_ipv6_in_guest


class _Svc:
    def __init__(self, name, guest, ports, unit=""):
        self.name, self.guest, self.ports, self.unit = name, guest, ports, unit


SERVICES = [_Svc("control-panel", "111", ("3001",), "control-panel.service")]
INV = {"names": {"111": "control-panel", "101": "jellyfin"}}
T1 = {"node_key": "T1", "title": "Inventory panel endpoints", "tool": "Shell", "description": "",
      "prompt_template": "List every /api/* endpoint the control panel backend on container 111 serves and where each is "
                         "implemented, and the config files it reads, so the probe step knows what to call."}


def test_the_inventory_step_is_rewritten_to_a_read_of_the_services_own_code():
    cmds, verify, notes = rewrite_inventory_service_routes(T1, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1572" in notes[0]["why"] and cmds == verify and len(cmds) == 1
    c = cmds[0]
    assert c.startswith("pct exec 111 -- sh -c '") and "/opt/control*" in c
    assert "(app|router)" in c and "--include=" in c and "node_modules" in c
    assert "fetch\\(" in c and "config" in c and "values never printed" in c
    assert "find /" not in c and "xargs" not in c
    assert c.rstrip("'").endswith("echo STATE-OK || echo STATE-WRONG") and notes[0]["expect"] == {c: "STATE-OK"}
    assert subprocess.run(["bash", "-n", "-c", c], capture_output=True, text=True).returncode == 0


def test_the_read_is_read_only_safe():
    from app.modules.assist_state_check import read_only_command
    cmds, _, _ = rewrite_inventory_service_routes(T1, ["x"], ["y"], SERVICES, INV)
    assert read_only_command(cmds[0])


def test_the_guest_can_come_from_the_service_word_alone_and_other_steps_are_left_alone():
    bare = {"title": "Map the backend's API routes", "tool": "Shell", "description": "the panel backend's routes and their handlers"}
    cmds, _, notes = rewrite_inventory_service_routes(bare, ["x"], ["y"], SERVICES, INV)
    assert notes and cmds[0].startswith("pct exec 111 -- ")
    for node in ({"title": "Fix Radarr endpoint backend", "tool": "Shell", "description": "return real data from /api/media-library"},
                 {"title": "Probe each endpoint authenticated", "tool": "Shell", "description": "curl every /api/* as the panel user"},
                 {"title": "Disable IPv6 on container 101", "tool": "Shell", "description": "the jellyfin guest"}):
        assert not rewrite_inventory_service_routes(node, ["x"], ["y"], SERVICES, INV)[2], node["title"]
    assert not rewrite_disable_ipv6_in_guest(T1, ["x"], ["y"], SERVICES, INV)[2]
    nowhere = {"title": "Inventory the endpoints", "tool": "Shell", "description": "of something"}
    assert not rewrite_inventory_service_routes(nowhere, ["x"], ["y"], [], {})[2]


def test_frame_run_runs_the_inventory_rewrite_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_inventory_service_routes(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_inventory_service_routes(node, cmds") < src.index("read_on_the_machine(cmds")


# §17.1602 — a step that CHANGES something is never rewritten into a read-only template.
def test_the_live_token_step_is_not_rewritten_into_a_read():
    """Live (c2792d4b T14, 2026-10-15): "Create a read-only Proxmox API token for the panel" matched "read … API"; the
    inventory read replaced its draft and would have recorded it done with no token created."""
    import json, pathlib
    from app.modules.supervised_runs import rewrite_probe_service_endpoints
    node = json.loads((pathlib.Path(__file__).parent / "fixtures" / "panel_t14_node_2026_10_15.json").read_text())
    cmds, ver, notes = rewrite_inventory_service_routes(node, ["pveum user add panel@pve"], [], SERVICES, INV)
    assert notes == [] and cmds == ["pveum user add panel@pve"]
    cmds, ver, notes = rewrite_probe_service_endpoints(node, ["x"], [], SERVICES, INV, logins=[{"name": "PANEL_PASSWORD"}])
    assert notes == [] and cmds == ["x"]


def test_read_only_is_an_adjective_and_a_read_step_still_is_one():
    node = {"title": "Check the read-only token", "description": "the panel has a read-only API token"}
    assert rewrite_inventory_service_routes(node, ["a"], [], SERVICES, INV)[2] == []
    still = {"title": "Inventory panel endpoints", "description": "read the panel backend's routes"}
    assert rewrite_inventory_service_routes(still, ["a"], [], SERVICES, INV)[2] != []
