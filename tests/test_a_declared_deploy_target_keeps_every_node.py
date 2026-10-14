"""§17.1549 — a declared deploy target keeps every build node on it.

Live, 5-section control-panel job 5b71ec89, 2026-10-14. The brief declared the
deploy target as container 111 (the panel), but the node "Add Palworld status
endpoint" named the *palworld* service — which the engine measured on VM 106 —
so `host_for` resolved VM 106 and the delivery wrote `/opt/palworld/status_server.py`
there and restarted the game server, instead of adding a route to the panel on
CT 111 that reads VM 106 via the Proxmox API.

A panel-aggregator node names the service it QUERIES, not the machine it deploys
to. When the job DECLARES a single deploy target (deploy-verb phrasing, §17.1540),
that target outranks the service a node merely names. Only an explicit guest id
in the node's own title overrides it.
"""
from __future__ import annotations

from app.modules import develop
from app.modules.service_truth import ServiceTruth

PANEL = ServiceTruth(guest="111", unit="control-panel.service", name="control-panel",
                     workdir="/opt/control-panel-backend", user="root")
PALWORLD = ServiceTruth(guest="106", unit="palworld.service", name="palworld",
                        workdir="/opt/palworld", user="steam", vm=True)
SERVICES = [PANEL, PALWORLD]

# deploy-verb phrasing naming container 111 → deploy_target_guest == "111"
BRIEF_DECLARES_111 = ("Build the control panel. Deploy every change to container 111 via the runner; "
                      "each endpoint is developed and deployed to container 111, its files pushed into "
                      "container 111 and the control-panel service on container 111 restarted.")
# a brief with no deploy-verb target (the panel is only surfaced, VM 106 only mentioned)
BRIEF_NO_TARGET = "The panel runs on container 111 and shows the Palworld server (VM 106) status."

NODE = {"title": "Add Palworld status endpoint", "tool": "CodeGen", "description": ""}


def test_the_declared_target_wins_over_the_named_service():
    # deploy_target_guest resolves 111, so the Palworld-named node still hosts on the panel.
    from app.modules.step_classify import deploy_target_guest
    assert deploy_target_guest(BRIEF_DECLARES_111) == "111"
    host = develop.host_for(NODE, SERVICES, brief=BRIEF_DECLARES_111)
    assert host is not None
    assert host.guest == "111" and host.workdir == "/opt/control-panel-backend"
    assert host.vm is False          # the panel LXC, not the Palworld VM


def test_an_explicit_guest_id_in_the_title_still_overrides():
    # a node that genuinely deploys ONTO VM 106 names it by id — that still wins.
    node = {"title": "Install the status agent on VM 106", "tool": "CodeGen", "description": ""}
    host = develop.host_for(node, SERVICES, brief=BRIEF_DECLARES_111)
    assert host is not None and host.guest == "106"


def test_without_a_declared_target_the_name_match_is_unchanged():
    # no deploy-verb target in the brief → the Palworld-named node hosts on VM 106 (prior behaviour).
    from app.modules.step_classify import deploy_target_guest
    assert deploy_target_guest(BRIEF_NO_TARGET) == ""
    host = develop.host_for(NODE, SERVICES, brief=BRIEF_NO_TARGET)
    assert host is not None and host.guest == "106"


def test_a_non_build_node_is_still_not_hosted():
    # a checkpoint that merely verifies is never developed, declared target or not.
    node = {"title": "Verify the Palworld endpoint responds", "tool": "Shell", "node_type": "checkpoint"}
    assert develop.host_for(node, SERVICES, brief=BRIEF_DECLARES_111) is None
