r"""§17.1569 — a supervised node's record opens with what was approved and ran, not the pre-rewrite draft.

Live (jobs 263e920a / 977a8a38): `output_text` began `## Run this` + the planner's original block
(`for item in ${T3}; do curl … | jq …`) while `## Executed on` held the engine capability's rewrite that
actually ran. The report step mirrored the dead command (§17.1567) and any reader would have believed it
was the step's plan. The frame — commands, verify, files, engine_fixed — is the approved truth.
"""
from __future__ import annotations

import inspect

from app.modules import supervised_runs as sr

DRAFT = ("## Run this\n```bash\nfor item in ${T3}; do curl -s -H 'X-Emby-Token: ${T1}' \"http://192.168.1.20:8096/Items/$item\" | jq '.'; done\n```")
FRAME = {"runbook": DRAFT,
         "commands": ["pct exec 101 -- sh -c 'python3 -c \"census\" \"<JELLYFIN_API_KEY>\" \"report\"'"],
         "verify": ["pct exec 101 -- curl -s -o /dev/null -w \"jellyfin HTTP %{http_code}\" http://127.0.0.1:8096/System/Info/Public"],
         "files": [{"path": "/tmp/in_ct_101.sh", "content": "#!/bin/bash"}],
         "engine_fixed": [{"why": "drafted the metadata check from the engine's own shape (§17.1554)"},
                          {"why": "read JELLYFIN_API_KEY inside guest 101 instead of asking"}]}


def test_the_record_head_is_the_frame_not_the_draft():
    head = sr._frame_runbook(FRAME, FRAME["commands"], FRAME["verify"])
    assert head.startswith("## Run this\n```bash\npct exec 101 -- sh -c 'python3 -c \"census\"")
    assert "for item in ${T3}" not in head and "jq" not in head
    assert "## Verify" in head and "/System/Info/Public" in head
    assert "## Files written" in head and "/tmp/in_ct_101.sh" in head
    assert "## The engine drafted this" in head and "§17.1554" in head and "instead of asking" in head


def test_the_full_record_keeps_the_executed_marker_and_a_consumer_reads_results_only():
    from app.modules.execution_agent import _result_of
    head = sr._frame_runbook(FRAME, FRAME["commands"], FRAME["verify"])
    record = sr._executed_report(head, "pve-runner", [{"command": FRAME["commands"][0], "output": "RESULT: 20 item(s) still lack", "exit": 0}],
                                 "$ " + FRAME["verify"][0] + "\njellyfin HTTP 200")
    assert "## Executed on pve-runner (supervised, operator-approved)" in record
    assert record.index("## Run this") < record.index("## Executed on")
    out = _result_of(record)
    assert "RESULT: 20 item(s) still lack" in out and "jellyfin HTTP 200" in out and "python3 -c" not in out


def test_resolve_run_builds_the_head_from_the_frame_and_falls_back_to_the_draft_when_nothing_ran():
    src = inspect.getsource(sr.resolve_run)
    assert "_head = _frame_runbook(waiting, runnable, verify_cmds) if runnable else runbook" in src
    assert "_executed_report(_head, spec.name, executed, verify_out)" in src
    # an empty frame renders an empty block, never the draft
    assert sr._frame_runbook({}, [], []) == "## Run this\n```bash\n```"
