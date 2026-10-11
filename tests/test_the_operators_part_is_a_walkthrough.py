"""§17.1458 — in Auto mode the operator's part of the plan is a walkthrough the run waits on, then checks.

Live (2026-10-10): ADD150 advertised the Tailscale subnet from the host; approving it needed the operator's own
Tailscale admin console, nothing walked them through it, and the step was recorded FAILED. A `tool: human` step
was worse — Auto mode recorded it done ("Skipped: human review not required in auto mode"). ADD152 (the
router port-forward, in the operator's phone app) is wholly theirs.
"""
import asyncio
import json
import pathlib

import pytest

from app.modules import operator_steps as ops

FIX = pathlib.Path("tests/fixtures")
ADD150 = json.loads((FIX / "add150_node_2026_10_10.json").read_text())
ADD151 = json.loads((FIX / "add151_node_2026_10_10.json").read_text())
ADD152 = json.loads((FIX / "add152_node_2026_10_10.json").read_text())


def test_add150_is_part_engine_part_operator():
    part = ops.operators_part(ADD150)
    assert "admin console" in part and "operator" in part
    assert not ops.is_wholly_operators(ADD150), "it gives the engine `tailscale set` to run"
    assert "tailscale status --json | grep -c 192.168.1.0/24" in ops.checks(ADD150)


def test_add152_is_wholly_the_operators():
    assert ops.is_wholly_operators(ADD152)
    assert "My Spectrum app" in ops.operators_part(ADD152)
    assert ops.checks(ADD152) == ["grep -A3 'group game' /etc/pve/firewall/cluster.fw"]


def test_the_firewall_step_is_the_engines():
    assert not ops.is_wholly_operators(ADD151)


def test_a_human_tagged_step_is_the_operators_whole():
    node = {"node_key": "T9", "title": "Plug the NAS in", "tool": "human",
            "description": "Plug the NAS into the switch. Done when `ip neigh show 192.168.1.40` shows it."}
    assert ops.is_wholly_operators(node) and ops.operators_part(node).startswith("Plug the NAS")


class _DB:
    def __init__(self):
        self.sql = []

    async def execute(self, stmt, params=None):
        self.sql.append((str(stmt), params or {}))

        class R:
            rowcount = 1
        return R()


@pytest.fixture
def machine(monkeypatch):
    from app.modules import supervised_runs as sr
    state = {"verdict": "confirmed"}

    async def channel(db):
        return (object(), {})

    async def run_verify(spec, cmds, env):
        return "== V1 ==\n2\n", [{"id": "V1", "ok": True}]

    async def verdicts(title, cmds, pasted, expects=None):
        return [{"id": "V1", "verdict": state["verdict"]}]
    monkeypatch.setattr(sr, "channel", channel)
    monkeypatch.setattr(sr, "run_verify", run_verify)
    monkeypatch.setattr(sr, "_verify_verdicts", verdicts)
    return state


def _waiting():
    return {"kind": ops.KIND, "title": ADD150["title"], "part": ops.operators_part(ADD150),
            "verify": ["tailscale status --json | grep -c 192.168.1.0/24"]}


def test_done_is_recorded_only_when_the_machine_confirms(machine):
    db = _DB()
    res = asyncio.run(ops.resolve_operator(db, "j", "ADD150", "done", _waiting()))
    assert res["outcome"] == "done" and "## Checked on the machine" in db.sql[-1][1]["out"]
    machine["verdict"] = "contradicted"
    db2 = _DB()
    res2 = asyncio.run(ops.resolve_operator(db2, "j", "ADD150", "done", _waiting()))
    assert res2["outcome"] == "not_yet" and not db2.sql, "nothing is written when the machine says no"


def test_skip_and_bad_choice(machine):
    assert asyncio.run(ops.resolve_operator(_DB(), "j", "ADD152", "skip", {}))["outcome"] == "skipped"
    assert asyncio.run(ops.resolve_operator(_DB(), "j", "ADD152", "maybe", {}))["outcome"] == "bad_choice"


def test_the_engine_half_hands_the_rest_back():
    db = _DB()
    assert asyncio.run(ops.hand_back_engine_half(db, "j", ADD150, {"outcome": "failed"})) is True
    assert "SET status = 'pending'" in db.sql[0][0]
    assert asyncio.run(ops.hand_back_engine_half(_DB(), "j", ADD151, {"outcome": "failed"})) is False


def test_it_is_wired_end_to_end():
    ea = pathlib.Path("app/modules/execution_agent.py").read_text()
    dp = pathlib.Path("app/modules/decision_pause.py").read_text()
    sr = pathlib.Path("app/modules/supervised_runs.py").read_text()
    th = pathlib.Path("app/ui/static/views/theater.js").read_text()
    assert "op_node = await _ops.pending_operator_step(db, job_id)" in ea
    assert "frame = await _ops.frame_operator(op_node, upstream=up_block, facts=" in ea   # §17.1464 adds facts/also
    assert 'if waiting.get("kind") == "yours":' in dp and "hand_back_engine_half(db, job_id" in dp
    assert "_ops.is_wholly_operators(node)" in sr and "_ops.ENGINE_HALF_DONE" in sr
    assert 'if (d && d.kind === "yours") return showOperatorStep(d);' in th
