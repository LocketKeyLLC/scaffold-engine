"""§17.1052 — deterministic "this worked" facts from the operator's pastes
(app/modules/assist_memory.worked_command_facts).

Live: `node server.js` printed "Backend API running on port 3001" at 12:24 and
the model-derived memory recorded the failures around it; six hours later the
fix loop chased `index.js` from the package file.
"""
from __future__ import annotations

import app.modules.execution_agent  # noqa: F401 — load-bearing (real app.database)
from app.modules.assist_memory import worked_command_facts

PASTE = """root@pve:~# pct exec 111 -- bash -c 'cd /opt/control-panel-backend && node server.js'
Backend API running on port 3001
^Croot@pve:~# pct exec 111 -- cat /var/log/control-panel.log
cat: /var/log/control-panel.log: No such file or directory
root@pve:~# pct exec 111 -- systemd-run --unit=control-panel --working-directory=/opt/control-panel-backend node server.js
Running as unit: control-panel.service
root@pve:~# pct exec 111 -- systemctl is-active control-panel
active
root@pve:~# pct start 120
root@pve:~# qm status 110
status: stopped
"""


def test_commands_followed_by_success_output_become_facts_and_failures_do_not():
    facts = worked_command_facts(PASTE, node_key="T37")
    assert facts[0].startswith("Worked at T37: `pct exec 111 -- bash -c 'cd /opt/control-panel-backend && node server.js'` → Backend API running on port 3001")
    assert any("systemd-run --unit=control-panel" in f and "Running as unit" in f for f in facts)
    assert any("systemctl is-active control-panel` → active" in f for f in facts)
    joined = "\n".join(facts)
    assert "cat /var/log" not in joined          # a read command
    assert "pct start 120" not in joined         # silence is not success
    assert "qm status 110" not in joined         # "stopped" is a failure word


def test_no_prompt_lines_means_no_facts():
    assert worked_command_facts("just some prose about the build") == []
    assert worked_command_facts("") == []


def test_both_distillers_fold_in_the_worked_facts():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "app/modules/assist_memory.py").read_text()
    turn = src[src.index("async def derive_turn_memory("):]
    assert "worked_command_facts(msg, node_key=node_key)" in turn
    sub = src[src.index("async def capture_session_facts("):src.index("async def derive_turn_memory(")] if src.index("async def capture_session_facts(") < src.index("async def derive_turn_memory(") else src[src.index("async def capture_session_facts("):]
    assert "worked_command_facts(evidence, node_key=node_key)" in sub
