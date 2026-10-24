"""§17.1804 — a fact a machine holds about itself is read off the machine once reachable, never asked of the operator.

Live (job f9ca3ee1): T1 "Ask the operator how many old laptops exist and their model, RAM, core count, and disk size",
with the capacity estimate, comparison and approval on it and the OS install walkthrough + SSH key on the approval.
The operator does not know the models; the engine reads them after walking them through the install.
"""
from __future__ import annotations

import copy

from app.modules import dag_generator as g


def _t(i, name, desc, deps, tool="LLM"):
    return {"id": i, "name": name, "notes": desc, "depends_on": deps, "tool": tool, "type": "action"}


LIVE = [
    _t("T1", "Inventory laptop hardware", "Ask the operator how many old laptops exist and their model, RAM, core "
       "count, and disk size. Produce a structured inventory list.", []),
    _t("T2", "Recommend operating system", "Recommend a lightweight Linux OS for k3s workers. Operator chooses.", ["T1"]),
    _t("T3", "Choose compute distribution strategy", "Operator decision: how to distribute AI compute.", ["T1"]),
    _t("T4", "Measure laptop compute capacity", "Estimate each laptop's usable RAM and CPU after k3s overhead.", ["T1"]),
    _t("T5", "Research parallel serving gain", "Quantify expected gain.", ["T3", "T4"]),
    _t("T6", "Research VM 110 upgrade option", "Estimate cost and gain of a bigger VM 110.", []),
    _t("T7", "Compare cluster vs upgrade", "Recommend the winner.", ["T5", "T6"]),
    _t("T8", "Present plan for approval", "Get explicit approval before any changes.", ["T7", "T3"]),
    _t("T9", "Write OS install walkthrough", "Step-by-step install of the chosen OS on each laptop, BIOS boot from USB.",
       ["T2", "T8"]),
    _t("T10", "Guide laptop LAN setup", "Connect each laptop to the LAN, enable SSH, report the IP.", ["T9"]),
    _t("T11", "Install engine SSH key", "The engine copies its SSH public key to each laptop.", ["T10"], "Shell"),
    _t("T12", "Install k3s control plane", "Install k3s server on VM 110.", ["T8"], "Shell"),
    _t("T14", "Join laptops as workers", "Run k3s agent on each laptop.", ["T11", "T12"], "Shell"),
]


def _ancestors(tasks, tid):
    by = {t["id"]: t for t in tasks}
    seen, stack = set(), list(by[tid]["depends_on"])
    while stack:
        d = stack.pop()
        if d not in seen:
            seen.add(d)
            stack.extend(by[d]["depends_on"])
    return seen


def test_the_live_plan_reads_the_hardware_after_the_laptops_are_reachable():
    tasks = copy.deepcopy(LIVE)
    changes = g.read_machine_facts_instead_of_asking(tasks)
    assert changes == ["T1 reads each laptop's hardware after T11 instead of asking the operator (setup chain T10, "
                       "T11, T2, T9 no longer waits on it)"]
    by = {t["id"]: t for t in tasks}
    t1 = by["T1"]
    assert t1["name"] == "Read laptop hardware" and t1["tool"] == "Shell" and t1["depends_on"] == ["T11"]
    assert "lscpu" in t1["notes"] and "never asked" in t1["notes"]
    assert by["T2"]["depends_on"] == [] and by["T9"]["depends_on"] == ["T2"]          # setup waits on nothing after
    for uses in ("T4", "T7", "T8", "T12", "T14"):                                       # all that uses facts: after
        assert "T1" in _ancestors(tasks, uses) and "T11" in _ancestors(tasks, uses), uses
    for setup in ("T2", "T9", "T10", "T11"):
        assert "T1" not in _ancestors(tasks, setup) and "T8" not in _ancestors(tasks, setup), setup
    assert g.read_machine_facts_instead_of_asking(tasks) == []                          # idempotent


def test_an_ask_without_machine_facts_or_without_a_way_in_is_left_alone():
    one_fact = [_t("A", "Ask budget", "Ask the operator their budget and the model they prefer.", []),
                _t("B", "Install engine SSH key", "Copy the engine's SSH key to each machine.", [], "Shell")]
    assert g.read_machine_facts_instead_of_asking(one_fact) == []
    no_reach = [_t("A", "Inventory", "Ask the operator for each laptop's model, RAM and disks.", [])]
    before = copy.deepcopy(no_reach)
    assert g.read_machine_facts_instead_of_asking(no_reach) == [] and no_reach == before


def test_the_planner_is_told_the_rule():
    assert "NEVER plan a step that asks the operator for a fact a machine holds about itself" in g.DAG_SYSTEM


async def test_generate_dag_stores_the_read_not_the_question():
    from unittest.mock import AsyncMock, MagicMock, patch
    from uuid import uuid4

    async def _stub(brief_data, route_kwargs):
        return {"dag_data": {"strategy": "sequential", "tasks": [
                    {**t, "inputs": [], "outputs": [f"o_{t['id']}"], "domain": None, "assigned_model": None,
                     "is_deliverable": t["id"] == "T14"} for t in copy.deepcopy(LIVE)]},
                "raw_text": "", "model": "m", "duration_ms": 0, "warnings": [], "error": None, "attempts": 1,
                "validator_calls": 0}
    brief = {"title": "Kubernetes cluster from old laptops", "description": "Cluster old laptops.", "goals": ["A cluster."]}
    row = MagicMock()
    row.first.return_value = ("planning", brief, None, None, None, 0, False)
    inserted: list[dict] = []

    async def _execute(stmt, params=None, *a, **k):
        if params and "node_key" in params and "prompt_template" in params:
            inserted.append(dict(params))
        return row
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    with patch.object(g, "_generate_dag_with_validator", _stub), patch.object(g, "_fail_job", new_callable=AsyncMock):
        try:
            await g.generate_dag(str(uuid4()), db)
        except Exception:                                    # the persistence tail is not under test
            pass
    t1 = {p["node_key"]: p for p in inserted}["T1"]
    assert "lscpu" in t1["prompt_template"] and "Ask the operator" not in t1["prompt_template"]
