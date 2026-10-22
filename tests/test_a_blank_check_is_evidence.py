"""§17.1738 — a check that ran and printed nothing is evidence, not an unknown.

Live (job 7d30fe4f T3/T4/T5, 2026-10-21): `curl -s …/Users/Public | grep -E -o '"Name": *"mark"'` printed nothing on a
Jellyfin that hides users from its login screen, and each step was recorded done on exit codes.
"""
from __future__ import annotations

import pytest

from app.modules import supervised_runs as sr

LIVE = "pct exec 101 -- sh -c 'curl -s http://127.0.0.1:8096/Users/Public | grep -E -o \"\\\"Name\\\": *\\\"mark\\\"\"'"


def _ran(*cmds):
    return [{"id": f"V{i}", "command": c, "ok": True, "ran": True} for i, c in enumerate(cmds, 1)]


@pytest.mark.smoke
def test_the_live_blank_filter_is_contradicted():
    out = sr.blank_checks("== V1 ==\n\n", _ran(LIVE))
    assert len(out) == 1 and out[0]["verdict"] == "contradicted" and "printed NOTHING" in out[0]["reason"]


@pytest.mark.smoke
def test_a_filter_that_found_it_passes():
    assert sr.blank_checks('== V1 ==\n"Name":"mark"\n', _ran(LIVE)) == []


@pytest.mark.smoke
def test_silent_by_design_checks_are_not_judged_blank():
    cmds = ("pct exec 101 -- sh -c 'curl -s x | grep -q mark'", "test -f /etc/x", "systemctl is-active --quiet jellyfin")
    assert sr.blank_checks("== V1 ==\n\n== V2 ==\n\n== V3 ==\n\n", _ran(*cmds)) == []


@pytest.mark.smoke
def test_all_blank_non_filter_checks_confirm_nothing_and_one_answer_is_enough():
    cmds = ("pct exec 101 -- cat /etc/jellyfin/system.xml", "pct exec 101 -- ls /var/lib/jellyfin")
    assert sr.blank_checks("== V1 ==\n\n== V2 ==\n\n", _ran(*cmds))[0]["reason"].startswith("all 2 check(s)")
    assert sr.blank_checks("== V1 ==\n\n== V2 ==\nroot\n", _ran(*cmds)) == []


@pytest.mark.smoke
def test_a_check_that_never_ran_is_left_to_the_never_ran_rule():
    ran = [{"id": "V1", "command": LIVE, "ok": False, "ran": False, "why": "refused"}]
    assert sr.blank_checks("", ran) == []


def test_the_run_path_calls_it():
    import inspect
    src = inspect.getsource(sr)
    assert "_against = list(_against) + blank_checks(pasted, ran)" in src


async def _resolve(verify_out: str):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    cmd = "pct exec 101 -- sh -c 'curl -s --fail-with-body -X POST http://127.0.0.1:8096/Users/New'"
    waiting = {"kind": "run", "node_key": "T3", "title": "Create user mark", "description": "Create the user.",
               "prompt_template": "", "runbook": "## Run this", "commands": [cmd], "verify": [LIVE], "refused": []}
    with patch.object(sr, "channel", AsyncMock(return_value=(SimpleNamespace(name="pve-runner"), {"allow": ["ANY"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=([cmd], [])), \
         patch("app.modules.assist_supervised.run_block",
               new=AsyncMock(return_value=[{"command": cmd, "ok": True, "exit": 0, "output": '{"Name":"mark"}'}])), \
         patch("app.modules.assist_local_runner.run_probes",
               new=AsyncMock(return_value=(verify_out, [{"id": "V1", "command": LIVE, "ok": True, "ran": True}]))), \
         patch.object(sr, "_verify_verdicts", AsyncMock(return_value=[{"verdict": "unknown", "reason": "blank"}])), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        return await sr.resolve_run(db, "j", "T3", "run", waiting)


@pytest.mark.asyncio
async def test_the_live_step_with_a_blank_check_is_not_done():
    out = await _resolve("== V1 ==\n\n")
    assert out["node_status"] == "failed", out.get("reason")


@pytest.mark.asyncio
async def test_the_same_step_whose_check_found_it_is_done():
    out = await _resolve('== V1 ==\n"Name": "mark"\n')
    assert out["node_status"] == "done", out.get("reason")
