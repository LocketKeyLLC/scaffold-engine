"""§17.1632/§17.1633 — the engine can read the server it deployed.

Live (job 0fb4ea7f VS2, 2026-10-18): the operator asked the engine to verify the server's state, and the read-only
block it drafted came back with two wrong answers:

- §17.1632: `systemctl is-active scaffold-lan-gate` was refused as "not a unit anything the engine holds names". That
  unit is the engine's OWN: scripts/scaffold-lan-gate.service, installed by scripts/deploy_on_guest.sh.
- §17.1633: `git -C /opt/scaffold-engine log`, `docker exec ollama ollama list` and `docker exec scaffold-orchestrator
  printenv` were judged writes, so the block got `set -e`: one failed read would end it before the rest.
"""
from __future__ import annotations

from app.modules.assist_state_check import read_only_command
from app.modules import supervised_runs as sr

READS = ["git -C /opt/scaffold-engine log -1 --format=%h",
         "docker exec ollama ollama list",
         "docker exec scaffold-orchestrator printenv | grep -E '^MODEL_' | sort",
         "docker exec -u root -e X=1 scaffold-orchestrator cat /code/app/config.py",
         "docker exec --workdir=/code scaffold-orchestrator ls"]
WRITES = ["git -C /opt/scaffold-engine checkout --detach origin/main",
          "git -C /opt/scaffold-engine pull",
          "docker exec ollama ollama pull qwen3:4b",
          "docker exec ollama ollama rm qwen3:4b",
          "docker exec scaffold-orchestrator rm -rf /tmp/x",
          "docker exec -it scaffold-postgres psql -U scaffold -c 'drop table x'",
          "docker exec -u root scaffold-orchestrator sh -c 'echo x > /tmp/y'",
          "docker exec",
          "docker rm -f ollama"]


def test_a_read_inside_a_container_or_a_repo_is_a_read():
    for c in READS:
        assert read_only_command(c), c


def test_a_write_through_the_same_doors_is_still_a_write():
    for c in WRITES:
        assert not read_only_command(c), c


def test_the_engine_knows_the_units_it_ships():
    assert {"scaffold-lan-gate", "scaffold-oom-watcher", "scaffold-host-oom-watcher"} <= sr.engine_shipped_units()


def test_its_own_unit_is_not_refused_and_a_made_up_one_still_is():
    node = {"node_key": "VS2", "title": "VM 110: verify the server engine's state",
            "description": "Read only.", "prompt_template": "the LAN gate unit scaffold-lan-gate is active"}
    found = sr.unsourced_service_name(["systemctl is-active scaffold-lan-gate",
                                    "systemctl is-active scaffold-lan-gaet"], [], {}, node)
    assert [r["command"] for r in found] == ["systemctl is-active scaffold-lan-gaet"], found
