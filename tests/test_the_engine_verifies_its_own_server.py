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


# §17.1634 — the model's own `set -euo pipefail` in a read-only block (VS2's redraft after §17.1633)
VS2_LINES = """set -euo pipefail
echo "--- LAN gate unit ---"
systemctl is-active scaffold-lan-gate
echo "--- PLACEMENT.md P40 claim ---"
grep -i 'P40' /opt/scaffold-engine/PLACEMENT.md
echo "--- Final verification ---"
git -C /opt/scaffold-engine log -1 --format=%h"""


def test_a_shell_option_does_not_make_a_read_a_write_or_end_the_reads(tmp_path):
    import subprocess
    from app.modules import runbook_templates as rt
    assert rt.strict_line(VS2_LINES).startswith("set +e")
    body = rt.read_only_remote(VS2_LINES)
    assert "set -euo pipefail" not in body and body.count("\n") == VS2_LINES.count("\n") - 1
    # bash-level: the unit is inactive and the grep finds nothing -- both are answers, and the last read still runs
    bindir = tmp_path / "bin"; bindir.mkdir()
    for name, out in (("systemctl", 'echo inactive; exit 3'), ("grep", "exit 1"), ("git", "echo 1cb00182")):
        f = bindir / name; f.write_text(f"#!/bin/bash\n{out}\n"); f.chmod(0o755)
    script = rt.strict_line(VS2_LINES) + "\n" + body + "\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": f"{bindir}:/usr/bin:/bin"})
    assert "1cb00182" in r.stdout and r.returncode == 0, r.stdout + r.stderr
    # a block that WRITES keeps its own options and `set -e`
    writes = "set -euo pipefail\napt-get install -y jq"
    assert rt.strict_line(writes) == "set -e" and rt.read_only_remote(writes) == writes


def test_the_shell_option_lines_are_only_option_lines():
    from app.modules.runbook_templates import _SHELL_OPTIONS_RE as R
    for ln in ("set -euo pipefail", "set -e", "set +x", "set -o pipefail", "set -eu -o pipefail", "set -e  # strict"):
        assert R.match(ln), ln
    for ln in ("set", "set -- a b", "settings=1", "set -e; rm -rf /x", "set -euo pipefail && reboot"):
        assert not R.match(ln), ln


def test_the_rendered_read_only_script_carries_no_errexit_of_its_own():
    from app.modules import runbook_templates as rt
    body = rt.render(rt.REACH_VM_SSH_AND_RUN, {"GID": "110", "STEP": "VS2", "REMOTE_COMMANDS": VS2_LINES,
                                              "GUEST_USER_NAME": "AI_USER", "NEEDS_KEY": "yes"})
    remote = body.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
    assert remote.startswith("set +e; trap 'exit 0' EXIT") and "set -euo pipefail" not in remote, remote[:300]
