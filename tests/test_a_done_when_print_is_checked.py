"""§17.1630 — what a step's "Done when" names is what the run is judged by, and what the frame shows.

Live, job 0fb4ea7f, 2026-10-17:

- FB1 said "Done when it prints `fallback_setting deepseek-v4-pro:cloud` and `model deepseek-v4-pro:cloud success
  True fallback_used True`". Ollama was down, the run printed `success False … All connection attempts failed`,
  exited 0, and the step was recorded done: those texts are not commands, so nothing compared them.
- OL2's frame listed one check, `qm status 110`, while two in-guest checks from its own "Done when" ran after it:
  §17.1470/§17.1625 added them at the run only. The operator approves the frame, so it must show them.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import supervised_runs as sr
from app.modules.operator_steps import expected_prints, missing_prints

A = "fallback_setting deepseek-v4-pro:cloud"
B = "model deepseek-v4-pro:cloud success True fallback_used True"
FB1 = {"title": "VM 110: test the cloud fallback end to end", "description": "Live check.",
       "prompt_template": f"Inside VM 110: …\nDone when it prints `{A}` and `{B}` with a non-empty text."}
FAILED = (f"{A}\nmodel deepseek-v4-pro:cloud success False fallback_used True retries 3\n"
          "error All connection attempts failed\ntext ''")
PASSED = f"{A}\n{B} retries 1\nerror None\ntext 'OK'"


def test_the_texts_a_done_when_says_are_printed_are_read_and_its_checks_are_not():
    assert expected_prints(FB1) == [A, B]
    ol2 = {"prompt_template": 'Done when `docker ps --format "{{.Names}} {{.Status}}" | grep ollama` shows Up and '
                              '`curl -s http://127.0.0.1:11434/api/tags | head -c 300` lists models.'}
    assert expected_prints(ol2) == [], "a check command is a check, and 'shows Up' names no text"
    assert expected_prints({"prompt_template": "Done when `head -n 5 /opt/x/PLACEMENT.md` prints it."}) == []
    assert expected_prints({"prompt_template": "Print `x` first. Nothing here says Done."}) == []
    assert expected_prints({"description": "Done when `curl -s -o /dev/null -w '%{http_code}' http://h/` returns `200`."}) == ["200"]


def test_a_missing_text_is_missing_whatever_the_spacing():
    assert missing_prints([A, B], FAILED) == [B]
    assert missing_prints([A, B], PASSED.replace(" ", "  ")) == []


async def _run(output):
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    cmd = 'MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="aedefruscio" bash /tmp/in_vm_110.sh start'
    waiting = {"kind": "run", "node_key": "FB1", "title": FB1["title"], "description": FB1["description"],
               "prompt_template": FB1["prompt_template"], "runbook": "## Run this", "commands": [cmd],
               "verify": ["qm status 110"], "refused": []}
    with patch.object(sr, "channel", AsyncMock(return_value=(SimpleNamespace(name="pve-runner"), {"allow": ["ANY"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=([cmd], [])), \
         patch("app.modules.assist_supervised.run_block",
               new=AsyncMock(return_value=[{"command": cmd, "ok": True, "exit": 0, "output": output}])), \
         patch("app.modules.assist_local_runner.run_probes",
               new=AsyncMock(return_value=("== V1 ==\nstatus: running\n", [{"id": "V1", "command": "qm status 110"}]))), \
         patch.object(sr, "_verify_verdicts", AsyncMock(return_value=[{"verdict": "confirmed", "reason": "running"}])), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        return await sr.resolve_run(db, "j", "FB1", "run", waiting)


@pytest.mark.asyncio
async def test_the_live_failed_fallback_is_not_a_done_step():
    out = await _run(FAILED)
    assert out["node_status"] == "failed", out.get("reason")
    assert f"nothing printed `{B}`, which its Done when names" in out["reason"]


@pytest.mark.asyncio
async def test_the_live_passed_fallback_is_done():
    out = await _run(PASSED)
    assert out["outcome"] == "ran" and out["node_status"] == "done"


def test_the_frame_shows_the_checks_the_run_will_make():
    node = {"node_key": "X1", "title": "Restart Radarr in container 103",
            "description": "Done when `pct exec 103 -- systemctl is-active radarr` prints active."}
    runbook = ("## Run this\n\n```bash\npct exec 103 -- systemctl restart radarr\n```\n\n"
               "## Verify\n\n- the container runs: `pct status 103`\n")
    frame = sr.frame_run(node, runbook, SimpleNamespace(name="pve-runner"), {"allow": ["ANY"]})
    assert frame["verify"] == ["pct status 103", "pct exec 103 -- systemctl is-active radarr"]
    run = sr.with_own_checks(node, frame["verify"], frame["commands"])
    assert run == frame["verify"], "the run adds nothing the operator did not see"


# §17.1636 — the clause ends at its paragraph: live (PLF1), the operator's appended redraft note said "show `diff`"
PLF1_TEXT = ("Inside VM 110: correct the stale lines.\nDone when `grep '^MODEL_FALLBACK=' /opt/x/PLACEMENT.md` prints "
             "`MODEL_FALLBACK=deepseek-v4-pro:cloud` and `cat -n /opt/x/PLACEMENT.md` prints it.\n\n"
             "Operator review 2026-10-18 of the drafted block: build the new file in a temp copy, show `diff` against "
             "the original, then move it into place; `cat /etc/hostname` is not a check here.")


def test_an_appended_note_is_not_part_of_the_done_when():
    node = {"prompt_template": PLF1_TEXT}
    assert expected_prints(node) == ["MODEL_FALLBACK=deepseek-v4-pro:cloud"]
    from app.modules.operator_steps import checks
    assert checks(node) == ["grep '^MODEL_FALLBACK=' /opt/x/PLACEMENT.md", "cat -n /opt/x/PLACEMENT.md"]


def test_a_list_under_the_done_when_is_still_read():
    from app.modules.operator_steps import checks
    node = {"prompt_template": "Do it.\nDone when:\n\n- `systemctl is-active nginx` prints `active`\n- `curl -s http://h/` answers"}
    assert checks(node) == ["systemctl is-active nginx", "curl -s http://h/"] and expected_prints(node) == ["active"]
