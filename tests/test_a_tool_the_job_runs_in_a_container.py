"""§17.1614 — a tool the job runs only inside a container is not called bare; §17.1614b — a step's record keeps the
commands that ran inside the guest.

Live (job 0fb4ea7f T11 "Pull selected models", 2026-10-15): T9 put Ollama in Docker; three drafts of T11 ran a bare
`ollama pull` on the VM. T9's record held only `bash /tmp/in_vm_110.sh start`, never the in-guest commands.
"""
from __future__ import annotations

from app.modules import guest_evidence as ge
from app.modules import supervised_runs as sr

T9_FILE = ("cat > /tmp/in_vm_110_remote.sh <<'REMOTE'\nset -e\nexport DEBIAN_FRONTEND=noninteractive\n"
           "cd /opt/ollama\ndocker compose up -d\ndocker exec ollama ollama list\nREMOTE\nssh … start\n")
WAITING = {"files": [{"path": "/tmp/in_vm_110.sh", "content": T9_FILE}]}


def test_the_record_keeps_the_in_guest_commands():
    rec = sr._frame_runbook(WAITING, ["bash /tmp/in_vm_110.sh start"], ["qm status 110"])
    assert "## Ran inside the guest" in rec and "docker exec ollama ollama list" in rec
    assert "export DEBIAN_FRONTEND" not in rec


def test_a_bare_call_of_a_container_only_tool_is_refused_with_the_job_s_own_form():
    out = ge.tools_only_in_containers(sr._frame_runbook(WAITING, [], []),
                                      ["set -e\nollama pull hf.co/x/y:Q4_K_M"])
    assert len(out) == 1 and "docker exec ollama ollama pull hf.co/x/y:Q4_K_M" in out[0]["why"]


def test_the_container_form_and_a_block_that_installs_the_tool_pass():
    up = sr._frame_runbook(WAITING, [], [])
    assert ge.tools_only_in_containers(up, ["docker exec ollama ollama pull hf.co/x/y"]) == []
    assert ge.tools_only_in_containers(up, ["curl -fsSL https://ollama.com/install.sh | sh\nollama pull x"]) == []
    assert ge.tools_only_in_containers("no docker here", ["ollama pull x"]) == []


def test_it_is_registered_for_redraft():
    assert any("runs only inside the container" in m for m in sr._SHAPE_REFUSALS)
    assert sr._WHOSE_GAP["runs only inside the container"] == "drafter"
