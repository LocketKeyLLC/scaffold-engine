"""§17.1038 — plan-level value provenance (app/modules/plan_evidence.py).

The plan-only tier (§17.1034) showed the class: the planner wrote
``192.168.1.1`` and ``8211`` into task text the operator never gave, and every
downstream prompt then treated them as project context. These pin the pass
that marks such values ASSUMED before the plan is persisted.
"""
from __future__ import annotations

import pytest

from app.modules import plan_evidence as pe

BRIEF = {
    "title": "WireGuard on the home router",
    "description": "Run a WireGuard endpoint on the Proxmox host at 10.20.0.5 behind PPPoE.",
    "goals": ["Remote access to the LAN"],
    "constraints": ["Ubuntu 22.04.3 LTS on the host", "docs at https://docs.example-vendor.com/wg"],
}


def _task(**kw):
    t = {"id": "T1", "name": "Configure WireGuard peer", "type": "action",
         "inputs": [], "outputs": [], "depends_on": [], "tool": "Shell"}
    t.update(kw)
    return t


@pytest.fixture(autouse=True)
def _valve_on(monkeypatch):
    monkeypatch.setattr(pe.settings, "dag_value_provenance_enabled", True)


def test_a_value_the_brief_states_is_not_marked():
    t = _task(notes="Peer endpoint on 10.20.0.5; host runs 22.04")
    marked = pe.mark_assumed_values([t], pe.plan_corpus(BRIEF))
    assert marked == [] and "assumed_values" not in t
    assert t["notes"] == "Peer endpoint on 10.20.0.5; host runs 22.04"


def test_a_value_from_nowhere_is_marked_in_the_notes_and_carried_on_the_task():
    """The live class: the plan says 192.168.1.1 and port 8211; the brief never did."""
    t = _task(notes="Set the gateway to 192.168.1.1 and open port 8211")
    marked = pe.mark_assumed_values([t], pe.plan_corpus(BRIEF), job_id="j1")
    assert [u["value"] for u in t["assumed_values"]] == ["192.168.1.1", "8211"]
    assert marked[0]["id"] == "T1"
    assert t["notes"].startswith("Set the gateway to 192.168.1.1 and open port 8211\n\n")
    assert pe.ASSUMED_NOTE_PREFIX in t["notes"] and "`8211` (port)" in t["notes"]
    assert "confirm on the operator's system" in t["notes"]


def test_values_in_name_inputs_and_outputs_count_too():
    t = _task(name="Pin Node 20.11.1", inputs=["listener on port 3005"], outputs=["service reachable"])
    pe.mark_assumed_values([t], pe.plan_corpus(BRIEF))
    assert {u["value"] for u in t["assumed_values"]} == {"20.11.1", "3005"}
    assert t["notes"].startswith(pe.ASSUMED_NOTE_PREFIX)  # a task with no notes gets one


def test_placeholders_and_example_addresses_are_never_values():
    t = _task(notes="Set gateway to <ROUTER_IP>; test against 203.0.113.9 and 127.0.0.1")
    assert pe.mark_assumed_values([t], pe.plan_corpus(BRIEF)) == []


def test_a_url_on_a_host_the_brief_names_is_credited_and_a_foreign_one_is_not():
    t = _task(notes="See https://docs.example-vendor.com/wg/peers and https://blog.other.org/wg-guide")
    pe.mark_assumed_values([t], pe.plan_corpus(BRIEF))
    assert [u["value"] for u in t["assumed_values"]] == ["https://blog.other.org/wg-guide"]


def test_the_operator_request_and_the_research_record_are_provenance():
    corpus = pe.plan_corpus(BRIEF, input_text="my NAS is 10.20.0.40",
                            research_data={"feasibility": {"summary": "vendor recommends port 51821"}})
    t = _task(notes="Mount the NAS at 10.20.0.40 and listen on port 51821")
    assert pe.mark_assumed_values([t], corpus) == []


def test_the_pass_is_idempotent_and_the_note_does_not_credit_itself():
    t = _task(notes="Gateway 192.168.1.1")
    pe.mark_assumed_values([t], pe.plan_corpus(BRIEF))
    first = t["notes"]
    pe.mark_assumed_values([t], pe.plan_corpus(BRIEF))
    assert t["notes"] == first and first.count(pe.ASSUMED_NOTE_PREFIX) == 1
    assert [u["value"] for u in t["assumed_values"]] == ["192.168.1.1"]


def test_a_later_brief_that_states_the_value_clears_an_earlier_mark():
    t = _task(notes="Gateway 192.168.1.1")
    pe.mark_assumed_values([t], pe.plan_corpus(BRIEF))
    pe.mark_assumed_values([t], pe.plan_corpus({**BRIEF, "constraints": ["gateway is 192.168.1.1"]}))
    assert "assumed_values" not in t and t["notes"] == "Gateway 192.168.1.1"


def test_the_valve_turns_the_pass_off_without_touching_tasks(monkeypatch):
    monkeypatch.setattr(pe.settings, "dag_value_provenance_enabled", False)
    t = _task(notes="Gateway 192.168.1.1")
    assert pe.mark_assumed_values([t], pe.plan_corpus(BRIEF)) == []
    assert t["notes"] == "Gateway 192.168.1.1" and "assumed_values" not in t


def test_generate_dag_runs_the_pass_before_persisting():
    """Structural: the call sits inside generate_dag, above the INSERT."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "app/modules/dag_generator.py").read_text()
    body = src[src.index("async def generate_dag("):]
    call = body.index("mark_assumed_values(")
    insert = body.index("INSERT INTO dag_nodes")
    assert call < insert
    assert "plan_corpus(brief_data, input_text, research_data)" in body
