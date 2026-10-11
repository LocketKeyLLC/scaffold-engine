"""§17.1464 — what a step learned outlives it; the operator's steps in one place are one trip.

Live (2026-10-11): ADD4 learned the operator's Spectrum router app reserves — and so port-forwards — only devices
the router leased an address to (fixture: ADD4's real recap). The rule lived only in that recap. ADD152 (the
Palworld forward, fixed 192.168.1.106) then hit the same wall: "the app isn't showing an ip address for the vm and
wont allow me to reserve it. Shouldn't the engine of adjusted its plan to match what it had previously done? Even
grouping them together in the first place."
"""
import asyncio
import json
import pathlib

from app.modules import assist_agent as aa
from app.modules import operator_steps as ops

FIX = pathlib.Path("tests/fixtures")
RECAP = (FIX / "add4_recap_2026_10_10.txt").read_text()
ADD152 = json.loads((FIX / "add152_node_2026_10_10.json").read_text())


class _DB:
    def __init__(self, recap):
        self.recap = recap

    async def execute(self, stmt, params=None):
        recap = self.recap

        class R:
            def mappings(self_inner):
                class M:
                    def first(self2):
                        return {"progress_recap": recap}
                return M()
        return R()


def test_the_recaps_constraints_become_facts(monkeypatch):
    stored = {}

    async def set_env(*, session_id, facts, db):
        stored["facts"] = facts
    monkeypatch.setattr(aa, "set_environment", set_env)
    facts = asyncio.run(aa.promote_learned_constraints(session_id="s", node_key="ADD4", db=_DB(RECAP)))
    assert stored["facts"] == facts and all(f.startswith("Learned on ADD4: ") for f in facts)
    assert any("DHCP lease" in f and "Reserve is disabled" in f for f in facts), facts


def test_no_recap_promotes_nothing(monkeypatch):
    monkeypatch.setattr(aa, "set_environment", None)    # would raise if called
    assert asyncio.run(aa.promote_learned_constraints(session_id="s", node_key="X", db=_DB(""))) == []


def test_the_learned_rule_reaches_the_router_walkthrough():
    facts = [f"Learned on ADD4: {ln.strip(' -')}" for ln in RECAP.split("CONSTRAINTS:")[1].split("NEXT:")[0].splitlines() if ln.strip()]
    picked = ops.relevant_facts(ADD152, facts + ["LXC 103 (radarr) is running: 2 cores, 1024MB memory"])
    assert any("DHCP lease" in f for f in picked), picked
    assert not any("radarr" in f for f in picked)


def test_two_router_steps_are_one_place():
    other = {"node_key": "ADD4", "tool": "human",
             "description": "In the My Spectrum app on the operator's phone, create two port-forwarding rules."}
    assert "my spectrum app" in ops.surfaces(ADD152) and ops.surfaces(ADD152) & ops.surfaces(other)


def test_it_is_wired():
    src = pathlib.Path("app/modules/assist_agent.py").read_text()
    assert "await promote_learned_constraints(session_id=session_id, node_key=node_key, db=db)" in src
    ea = pathlib.Path("app/modules/execution_agent.py").read_text()
    assert "_also = await _ops.same_place_steps(db, job_id, op_node)" in ea and "facts=(_op_env or {}).get(\"facts\")" in ea
