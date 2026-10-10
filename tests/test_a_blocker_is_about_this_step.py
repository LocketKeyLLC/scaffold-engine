"""§17.1440 — the blocker vocabulary knows how this engine's own facts say something is broken, and a fact is
searched only for the steps it is about.

Before, "ENGINE MEASURED: VM 106 cannot resolve names", "VM 106 is stopped … reopened" and "QEMU guest agent is
not running" matched no symptom word, so those steps got no blocker research. Widening the vocabulary alone
made one container-120 fact ride along on 151 of 178 live steps; these tests pin the relevance that fixed it.
The fact strings are the live session's own.
"""
import pytest

from app.modules.assist_guide import blocker_research_query as q, _BLOCKER_FACT_RE, _is_symptom

VM106_DNS = "ENGINE MEASURED: VM 106 cannot resolve names (getent hosts deb.debian.org printed nothing)"
VM106_STOPPED = ("ENGINE MEASURED: VM 106 is stopped (`qm list`), though ADD9 'Start VM 106 (palworld-server)' "
                 "is recorded done -- reopened")
CT111_STOPPED = ("ENGINE MEASURED: container 111 is stopped (`pct list`), though ADD50 'Start container 111 "
                 "(control-panel)' is recorded done -- reopened")
AGENT = "Worked: `qm agent 106 ping` → QEMU guest agent is not running"
RUNNER = ("The local runner (pve-runner) refuses mutation verbs such as 'apt' and systemctl subcommands/flags "
          "(e.g. 'systemctl enable --now').")
CT120 = ("ENGINE MEASURED 2026-10-08 (ADD128 ran): container 120 (caddy-proxy, 192.168.1.26) is running; Caddy is "
         "active in it and serves defrusciohomelab.duckdns.org (Jellyfin/Prowlarr/Radarr/Sonarr routes, the "
         "control panel as the fallback); its DNS works. The only missing piece is the router forwarding TCP "
         "80/443 to 192.168.1.26 -- Let's Encrypt: \"Timeout during connect (likely firewall problem)\".")
# the live ledger's order (oldest first): VM 106 stopped, then its DNS measured
FACTS = {"facts": [AGENT, RUNNER, VM106_STOPPED, VM106_DNS, CT111_STOPPED, CT120]}


@pytest.mark.parametrize("fact", [VM106_DNS, VM106_STOPPED, CT111_STOPPED, AGENT, CT120,
                                  "On the Proxmox host (root@pve), nvidia-smi is not installed ('command not found').",
                                  "/opt/control-panel-backend/server does not exist"])
def test_the_engines_own_wording_is_a_symptom(fact):
    assert _is_symptom(fact)


def test_ordinary_state_is_not_a_symptom():
    for t in ("container 120 is running; Caddy is active in it", "Operator is creating LXC 120",
              "apt-get update now succeeds without error"):
        assert not _is_symptom(t), t
    assert _BLOCKER_FACT_RE.search("exited 2") and not _BLOCKER_FACT_RE.search("exited 0")


@pytest.mark.parametrize("title,desc,expect", [
    ("Give VM 106 a working nameserver", "", "cannot resolve names"),
    ("Verify QEMU Guest Agent responds on VM 106", "", "guest agent is not running"),
    ("Build control panel backend", "Node/Express backend for the control panel", "is stopped (`pct list`)"),
    ("Router app: forward TCP ports 80 and 443 to the Caddy proxy (192.168.1.26)", "", "Timeout during connect"),
])
def test_a_machine_fact_reaches_the_steps_on_that_machine(title, desc, expect):
    out = q(FACTS, [], title, task_text=desc)
    assert expect in out, out


@pytest.mark.parametrize("title,desc", [
    ("Add Jellyfin libraries for /media/movies and /media/tv", "In Jellyfin, add two libraries."),
    ("Add every public indexer Prowlarr listed, and connect it to Radarr", "systemctl restart prowlarr"),
    ("Set up Pi-hole as the home network's DNS", "apt install pihole"),
])
def test_no_fact_rides_along_on_an_unrelated_step(title, desc):
    assert q(FACTS, [], title, task_text=desc) == ""


def test_a_fact_about_the_engines_own_runner_is_never_searched():
    assert q({"facts": [RUNNER]}, [], "Install the engine's helper with systemctl and apt",
             task_text="apt install … ; systemctl enable --now helper") == ""


def test_a_different_machine_is_not_this_step():
    assert q({"facts": [CT111_STOPPED]}, [], "Give VM 106 a working nameserver") == ""
