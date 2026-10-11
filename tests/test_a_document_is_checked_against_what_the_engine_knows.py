"""§17.1480 — T38's write-up of the finished home lab.

Live (2026-10-11): T38 "Document architecture and setup" documented the Proxmox host at 192.168.1.129 (VM 110's
address — from a fact whose engine note said so, after the operator's words), stripped the measured Palworld
address 192.168.1.44 as "unsupported" (checked against the job row's ledger: 0 facts; the session held 40), opened
with "## Decision Nodes" for three long-settled questions (the planner's "emit a decision node" paragraph was in
every step's prompt), and was recorded done at 0.92 still showing the previous attempt's rejection.
"""
import pathlib

from app.modules import assist_inventory as inv
from app.modules import prompt_assembly as pa

LIVE = ("The router already has a reserved IP for Proxmox with MAC b4:af:15 mapped to 192.168.1.129 "
        "[engine: MAC b4:af:15 is VM 110 (ai-vm) per its config].")
ENV = {"system_state": {"110": {"kind": "vm", "attrs": {"name": "ai-vm"},
                                "devices": {"net0": "virtio=BC:24:11:B4:AF:15,bridge=vmbr0"}},
                        "host": {"kind": "host", "attrs": {"ip": "192.168.1.156"}}}}


def test_the_corrected_fact_reads_as_corrected():
    out = inv.rereconcile_facts([LIVE, "LXC 103 (radarr) is running"], ENV)
    assert out[0].startswith("VM 110 (ai-vm) (MAC b4:af:15) is at 192.168.1.129 — not the Proxmox host.")
    assert "The operator's words:" in out[0] and out[1] == "LXC 103 (radarr) is running"


def test_proxmox_as_a_feature_is_not_the_host():
    assert inv._HOST_RE.search("reserved IP for Proxmox with MAC")
    assert not inv._HOST_RE.search("the Proxmox firewall groups") and not inv._HOST_RE.search("Proxmox VE 9")


def test_a_step_prompt_never_asks_for_decision_nodes():
    brief = {"user_feedback": "Q: Preferred tech stack for the control panel?\nA: unsure",
             "ambiguities": ["Preferred tech stack for the control panel?"]}
    prompt = pa.build_base_prompt({"title": "Document architecture and setup", "description": "Write the docs."},
                                  brief, {})
    assert "emit a `decision` node" not in prompt and "STILL UNDECIDED" not in prompt


def test_the_output_is_checked_against_the_session_ledger_and_a_pass_clears_the_reason():
    src = pathlib.Path("app/modules/execution_agent.py").read_text()
    assert "environment=await _node_ledger(job_id, job)" in src
    assert 'verify_reason_for_db = reason if final_status == "failed" else ""' in src
