"""§17.689 — multi-turn decision deliberation.

A decision node whose deliverable is a CONCRETE artifact (a VLAN table) no
longer commits on the operator's first partial answer ("3 vlans"). The engine
assembles the artifact across turns — propose → the operator confirms/adjusts —
and commits the full, confirmed artifact only when the decision is resolved.

- deliberate_decision (LLM): needs_input vs resolved vs fail-soft error.
- run_step_decision (agent): gate on presented + decision node; map results.
- classify_turn: a decision step biases a choice/confirmation toward submit.

The model is always mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_agent, assist_guide


def _toolresp(args, success: bool = True):
    r = MagicMock()
    r.success = success
    if success and args is not None:
        call = MagicMock()
        call.arguments = args
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    r.text = ""
    return r


def _result(mappings_first=None):
    r = MagicMock()
    m = MagicMock()
    m.first.return_value = mappings_first
    m.all.return_value = []
    r.mappings.return_value = m
    return r


# ── deliberate_decision ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliberate_needs_input_returns_proposal():
    captured = {}

    async def _cap(messages, tools, **kw):
        captured["system"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        return _toolresp({
            "status": "needs_input",
            "message": "Here's a concrete 3-VLAN table: …\nConfirm or adjust.",
            "decision_record": "",
        })

    with patch.object(assist_guide.model_router, "tool_call", new=_cap):
        res = await assist_guide.deliberate_decision(
            title="Define VLAN plan",
            task_prompt="Produce a concrete table: VLAN ID, subnet/CIDR, DHCP scope.",
            latest_message="3 vlans",
        )
    assert res["status"] == "needs_input"
    assert "3-VLAN table" in res["message"]
    assert res["decision_record"] == ""
    # the operator's latest message reaches the model
    assert "3 vlans" in captured["user"]


@pytest.mark.asyncio
async def test_deliberate_resolved_returns_record():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_toolresp({
                          "status": "resolved",
                          "message": "Locked in a 3-VLAN plan.",
                          "decision_record": "VLAN 10 mgmt 10.0.10.0/24 …",
                      }))):
        res = await assist_guide.deliberate_decision(
            title="Define VLAN plan", task_prompt="Produce a table.",
            latest_message="looks good",
        )
    assert res["status"] == "resolved"
    assert res["decision_record"].startswith("VLAN 10 mgmt")


@pytest.mark.asyncio
async def test_deliberate_failsoft_on_unparsed():
    # No tool call → status='error' so the caller falls back to a plain commit.
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_toolresp(None, success=False))):
        res = await assist_guide.deliberate_decision(
            title="x", task_prompt="y", latest_message="3 vlans",
        )
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_deliberate_failsoft_on_bad_status():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_toolresp({"status": "weird", "message": "m"}))):
        res = await assist_guide.deliberate_decision(
            title="x", task_prompt="y", latest_message="m",
        )
    assert res["status"] == "error"


# ── run_step_decision (agent gate) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_step_decision_skips_non_decision_node():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={
        "status": "presented", "job_id": "j1", "metadata": {}, "notes": None,
        "title": "Run migration", "prompt_template": "…", "node_type": "task",
    })
    out = await assist_agent.run_step_decision(
        session_id="s1", node_key="T3", message="done", db=db,
    )
    assert out is None  # not a decision → fall back to plain commit


@pytest.mark.asyncio
async def test_run_step_decision_skips_unclaimed_step():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={
        "status": "pending", "job_id": "j1", "metadata": {}, "notes": None,
        "title": "Define VLAN plan", "prompt_template": "…", "node_type": "decision",
    })
    out = await assist_agent.run_step_decision(
        session_id="s1", node_key="T2", message="3 vlans", db=db,
    )
    assert out is None  # not 'presented' → let submit_step surface must-claim


@pytest.mark.asyncio
async def test_run_step_decision_needs_input():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={
        "status": "presented", "job_id": "j1", "metadata": {}, "notes": None,
        "title": "Define VLAN plan", "prompt_template": "Produce a table.",
        "node_type": "decision",
    })
    with patch.object(assist_agent, "_job_digest_for", new=AsyncMock(return_value="")), \
         patch.object(assist_guide, "deliberate_decision", new=AsyncMock(return_value={
             "status": "needs_input", "message": "proposal…", "decision_record": "",
         })):
        out = await assist_agent.run_step_decision(
            session_id="s1", node_key="T2", message="3 vlans", db=db,
        )
    assert out == {"status": "needs_input", "message": "proposal…",
                   "collect_kind": "decision"}


@pytest.mark.asyncio
async def test_run_step_decision_resolved_carries_record():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={
        "status": "presented", "job_id": "j1", "metadata": {}, "notes": None,
        "title": "Define VLAN plan", "prompt_template": "Produce a table.",
        "node_type": "decision",
    })
    with patch.object(assist_agent, "_job_digest_for", new=AsyncMock(return_value="")), \
         patch.object(assist_guide, "deliberate_decision", new=AsyncMock(return_value={
             "status": "resolved", "message": "done", "decision_record": "VLAN 10 …",
         })):
        out = await assist_agent.run_step_decision(
            session_id="s1", node_key="T2", message="looks good", db=db,
        )
    assert out["status"] == "resolved"
    assert out["decision_record"] == "VLAN 10 …"


@pytest.mark.asyncio
async def test_run_step_decision_failsoft_on_deliberation_error():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={
        "status": "presented", "job_id": "j1", "metadata": {}, "notes": None,
        "title": "Define VLAN plan", "prompt_template": "…", "node_type": "decision",
    })
    with patch.object(assist_agent, "_job_digest_for", new=AsyncMock(return_value="")), \
         patch.object(assist_guide, "deliberate_decision", new=AsyncMock(return_value={
             "status": "error", "message": "", "decision_record": "",
         })):
        out = await assist_agent.run_step_decision(
            session_id="s1", node_key="T2", message="3 vlans", db=db,
        )
    assert out is None  # error → plain single-turn commit


# ── classify_turn decision bias ────────────────────────────────────────────


# ── §17.690: gather steps (provide info one portion at a time) ─────────────


def test_collect_step_kind_classifies():
    # decision node → 'decision' regardless of task text
    assert assist_agent._collect_step_kind("decision", "anything") == "decision"
    # a "provides" task → 'gather'
    assert assist_agent._collect_step_kind("task",
        "Operator provides: exact model, disk inventory, GPU(s), NIC models.") == "gather"
    assert assist_agent._collect_step_kind("output",
        "Operator must provide static IP, netmask, gateway, DNS, hostname.") == "gather"
    # a plain action step → not a collect step
    assert assist_agent._collect_step_kind("task",
        "Run apt-get update && apt-get install proxmox-ve.") is None


@pytest.mark.asyncio
async def test_deliberate_gather_lists_missing_items():
    captured = {}

    async def _cap(messages, tools, **kw):
        captured["system"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        return _toolresp({
            "status": "needs_input",
            "message": "Captured: disk inventory. Still need: server model, GPU(s), NIC models.",
            "decision_record": "",
        })

    with patch.object(assist_guide.model_router, "tool_call", new=_cap):
        res = await assist_guide.deliberate_decision(
            title="Gather host hardware details",
            task_prompt="Operator provides: exact model, disk inventory, GPU(s), NIC models.",
            latest_message="sda 5.5T sas … sdc 558G sata …",
            kind="gather",
        )
    assert res["status"] == "needs_input"
    assert "Still need" in res["message"]
    # the gather system prompt (not the decision one) was used
    assert "SPECIFIC INFORMATION" in captured["system"]
    assert "one piece at a time" in captured["user"].lower() or "Gather step" in captured["user"]


@pytest.mark.asyncio
async def test_run_step_decision_gather_needs_input():
    # A 'gather' task node (node_type='task') with a partial answer must NOT
    # commit — run_step_decision returns needs_input with collect_kind='gather'.
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={
        "status": "presented", "job_id": "j1", "metadata": {}, "notes": None,
        "title": "Gather host hardware details",
        "prompt_template": "Operator provides: exact model, disk inventory, GPU(s), NIC models.",
        "node_type": "task",
    })
    with patch.object(assist_agent, "_job_digest_for", new=AsyncMock(return_value="")), \
         patch.object(assist_guide, "deliberate_decision", new=AsyncMock(return_value={
             "status": "needs_input", "message": "Still need: model, GPUs, NICs.",
             "decision_record": "",
         })) as delib:
        out = await assist_agent.run_step_decision(
            session_id="s1", node_key="T2", message="<lsblk output>", db=db,
        )
    assert out == {"status": "needs_input",
                   "message": "Still need: model, GPUs, NICs.", "collect_kind": "gather"}
    # deliberate_decision was called with kind='gather'
    assert delib.call_args.kwargs["kind"] == "gather"


@pytest.mark.asyncio
async def test_run_step_decision_plain_task_not_intercepted():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={
        "status": "presented", "job_id": "j1", "metadata": {}, "notes": None,
        "title": "Install Proxmox", "prompt_template": "Run the installer.",
        "node_type": "task",
    })
    out = await assist_agent.run_step_decision(
        session_id="s1", node_key="T5", message="done, 0 errors", db=db,
    )
    assert out is None  # not a decision, not a gather → plain single-turn commit


@pytest.mark.asyncio
async def test_classify_turn_decision_hint_appended():
    captured = {}

    async def _cap(messages, tools, **kw):
        captured["system"] = messages[0]["content"]
        call = MagicMock()
        call.arguments = {"intent": "submit"}
        r = MagicMock(); r.success = True; r.tool_calls = [call]
        return r

    with patch.object(assist_guide.model_router, "tool_call", new=_cap):
        await assist_guide.classify_turn(
            message="looks good", title="Define VLAN plan",
            task_prompt="Produce a table.", tool="LLM", is_decision=True,
        )
    assert "THIS STEP IS A DECISION" in captured["system"]
