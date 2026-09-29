"""§17.1201 — three different things a block can do, told apart.

The live run recorded two "failures" that were nothing of the kind:

* `qm config 106 | grep scsi0` exited **1** — `grep` finding nothing, which was
  the check PASSING (the disk had just been deleted). It aborted the block
  after the write it was verifying had already succeeded.
* `sleep 10 && qm agent 106 ping` came back "SSE stream ended without a
  response" — the connection dropped. The engine recorded `exited None` and
  failed the step, when the truthful answer is that nobody knows whether it
  ran, which is a different thing to retry.

So: a WRITE that exits non-zero fails the block; a READ that exits non-zero is
information; a transport error is an unknown outcome and says so.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw
from app.modules import supervised_runs as sr

pytestmark = pytest.mark.asyncio


def _spec():
    s = MagicMock(); s.name = "pve-runner"; s.headers = {"X-Runner-Token": "tok"}; return s


def _res(text_out):
    r = MagicMock(); r.text = text_out; r.structured = None; return r


async def _run(commands, replies):
    """Drive run_block with one canned runner reply per command."""
    calls = iter(replies)

    async def call_tool(spec, tool, payload):
        nxt = next(calls)
        if isinstance(nxt, Exception):
            raise nxt
        return _res(nxt)

    with patch("app.modules.mcp_client.call_tool", new=call_tool):
        return await sw.run_block(_spec(), commands)


async def test_a_read_that_answers_no_does_not_abort_the_block():
    """`grep` exits 1 when what it looked for is gone — on ADD81 that was the
    check passing, and it stopped the run with the write already applied."""
    done = await _run(
        ["qm set 106 --delete scsi0", "qm config 106 | grep scsi0", "qm start 106"],
        ["[exit 0]\n", "[exit 1]\n", "[exit 0]\n"],
    )
    assert len(done) == 3, "the block stopped at a read that merely answered 'no'"
    assert done[1]["informational"] is True and done[1]["ok"] is False
    assert done[0]["informational"] is False and done[2]["ok"] is True


async def test_a_write_that_fails_still_stops_the_block():
    done = await _run(
        ["qm set 106 --scsi0 local-lvm:100", "qm start 106"],
        ["[exit 2]\nstorage 'local-lvm' does not exist", "[exit 0]\n"],
    )
    assert len(done) == 1 and done[0]["ok"] is False and done[0]["informational"] is False


async def test_a_dropped_connection_is_an_unknown_outcome_not_an_exit_code():
    done = await _run(["qm agent 106 ping"], [RuntimeError("SSE stream ended without a response")])
    assert len(done) == 1
    e = done[0]
    assert e["unreachable"] is True and e["ok"] is False and e["informational"] is False
    assert e["exit"] is None and "SSE stream ended" in e["output"]


async def test_a_refusal_is_neither_informational_nor_unreachable():
    done = await _run(["reboot"], ["(refused by the local runner: host power — do that by hand)"])
    assert done[0]["refused"] is True
    assert done[0]["informational"] is False and done[0]["unreachable"] is False


async def test_the_block_succeeds_when_only_reads_answered_no():
    """The node must reach `done`: every write worked, and a read said 'not
    there', which is what the step was checking for."""
    waiting = {"kind": "run", "runbook": "## Run this\n```\nx\n```", "verify": [], "refused": [],
               "commands": ["qm set 106 --delete scsi0", "qm config 106 | grep scsi0"], "inputs": []}
    executed = [
        {"command": "qm set 106 --delete scsi0", "output": "", "exit": 0, "ok": True,
         "approval_id": "a", "refused": False, "informational": False, "unreachable": False},
        {"command": "qm config 106 | grep scsi0", "output": "", "exit": 1, "ok": False,
         "approval_id": "b", "refused": False, "informational": True, "unreachable": False},
    ]
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim)
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), {"allow": ["qm set"], "sudo": True}))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch("app.modules.assist_local_runner.run_probes", new=AsyncMock(return_value=("", []))):
        out = await sr.resolve_run(db, "j", "ADD81", "run", waiting)
    assert out["outcome"] == "ran", out.get("reason")


async def test_a_dropped_connection_says_the_outcome_is_unknown():
    waiting = {"kind": "run", "runbook": "## Run this\n```\nx\n```", "verify": [], "refused": [],
               "commands": ["qm agent 106 ping"], "inputs": []}
    executed = [{"command": "qm agent 106 ping", "output": "(runner error: SSE stream ended without a response)",
                 "exit": None, "ok": False, "approval_id": "a", "refused": False,
                 "informational": False, "unreachable": True}]
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim)
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), {"allow": ["qm set"], "sudo": True}))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch.object(sr, "diagnose_failure", new=AsyncMock(return_value="")):
        out = await sr.resolve_run(db, "j", "ADD65", "run", waiting)
    assert out["outcome"] == "failed" and out["unknown_outcome"] is True
    assert "connection to pve-runner dropped" in out["reason"]
    assert "UNKNOWN" in out["reason"]
    assert "exited None" not in out["reason"], "an exit code was invented for a call that never came back"


# ── the runner problem-solves, the way the walkthrough does ──────────────

async def test_a_failed_block_is_diagnosed_with_the_real_error_and_research():
    """The operator asked whether the runner can problem-solve like assist
    mode. It can, and it uses the SAME head: `assist_guide.generate_fix` needs
    no session, so Auto passes it the step context, the environment ledger and
    the actual failing command with its actual output."""
    seen = {}

    async def fake_fix(**kw):
        seen.update(kw)
        return {"fix": "## Diagnosis\nThe storage is named `local`, not `local-lvm`.\n", "status": "ok"}

    ctx = MagicMock()
    with patch("app.modules.assist_agent._assemble_ctx_for_node",
               new=AsyncMock(return_value=({"domain": "infra"}, ctx))), \
         patch("app.modules.runbook_inputs.job_environment",
               new=AsyncMock(return_value={"facts": ["the pool is called local"]})), \
         patch("app.modules.assist_guide.generate_fix", new=fake_fix):
        out = await sr.diagnose_failure(
            AsyncMock(), "j", "ADD81",
            [{"command": "qm set 106 --scsi0 local-lvm:100", "output": "storage 'local-lvm' does not exist",
              "exit": 2, "ok": False, "informational": False}],
            "`qm set …` exited 2")
    assert "local-lvm" in out and out.startswith("## Diagnosis")
    assert "qm set 106 --scsi0 local-lvm:100" in seen["error_text"], "the diagnosis got a summary, not the error"
    assert "storage 'local-lvm' does not exist" in seen["error_text"]
    assert seen["research"] is True, "research is what makes this more than re-reading the error"
    assert seen["environment"]["facts"] == ["the pool is called local"]
    assert seen["node_key"] == "ADD81"


async def test_the_diagnosis_skips_the_read_that_merely_answered_no():
    """It must diagnose the WRITE that failed, not the informational read that
    happened to come last."""
    seen = {}

    async def fake_fix(**kw):
        seen.update(kw); return {"fix": "x"}

    with patch("app.modules.assist_agent._assemble_ctx_for_node",
               new=AsyncMock(return_value=({}, MagicMock()))), \
         patch("app.modules.runbook_inputs.job_environment", new=AsyncMock(return_value={})), \
         patch("app.modules.assist_guide.generate_fix", new=fake_fix):
        await sr.diagnose_failure(AsyncMock(), "j", "ADD81", [
            {"command": "qm set 106 --scsi0 bad", "output": "no such storage", "exit": 2,
             "ok": False, "informational": False},
            {"command": "qm config 106 | grep scsi0", "output": "", "exit": 1,
             "ok": False, "informational": True},
        ], "reason")
    assert "qm set 106 --scsi0 bad" in seen["error_text"]
    assert "grep scsi0" not in seen["error_text"]


async def test_a_diagnosis_that_cannot_be_produced_never_loses_the_failure():
    """Fail-soft: the node must still be recorded failed with its reason."""
    with patch("app.modules.assist_agent._assemble_ctx_for_node",
               new=AsyncMock(side_effect=RuntimeError("model down"))):
        assert await sr.diagnose_failure(AsyncMock(), "j", "ADD81", [
            {"command": "x", "output": "y", "exit": 1, "ok": False, "informational": False}], "r") == ""
    assert await sr.diagnose_failure(AsyncMock(), "j", "ADD81", [], "r") == ""
