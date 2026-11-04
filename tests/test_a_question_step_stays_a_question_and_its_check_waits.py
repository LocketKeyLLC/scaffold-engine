"""§17.1902 — a plan pass that makes a step the operator's question (`decision`) says so on the node, and a step that
CHECKS what the operator's keys make happen keeps waiting on them.

Live (follow-up 5b4880c0, 2026-10-25 -> 11-04): §17.1769 made "Run end-to-end paper trade" the operator's "Are the
end-to-end paper trade keys stored in the engine's secrets?" -- a `decision` in the plan -- but the edit of the
existing node carried title, text, tool and dependencies, never the type: the node stayed a `task`. On 2026-11-04 it
ran as an LLM step, wrote a runbook ("store ALPACA_API_KEY_ID …"), passed its second attempt, and the job reported
`completed` -- with neither Alpaca key in the engine's secrets. And T11 "Validate end-to-end result" (read the audit
trail of the trade, confirm the P&L) had been unhooked from the keys step with the code steps, ran beside it, and
"validated" a trade that never happened."""
from __future__ import annotations

import copy
import inspect

from app.modules import engine_plan, node_editor

BRIEF = {"title": "Paper-trading research lab in scaffold engine",
         "description": "Add a paper-trading research lab to the scaffold engine's own codebase.",
         "goals": ["Add a paper-trading research lab module to this repository's code."]}

PLAN = [
    {"id": "T1", "name": "Build execution entry point", "tool": "CodeGen", "type": "action", "depends_on": [],
     "notes": "Python module `paper_execution_entry_point.py`."},
    {"id": "T2", "name": "Store the paper-trading keys in the existing secrets store", "tool": "Shell",
     "type": "action", "depends_on": ["T1"], "notes": "Store ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY."},
    {"id": "T3", "name": "Write tests for the entry point", "tool": "CodeGen", "type": "action", "depends_on": ["T2"],
     "notes": "Python test module `tests/test_paper_entry.py`."},
    {"id": "T4", "name": "Validate end-to-end result", "tool": "LLM", "type": "action", "depends_on": ["T2"],
     "notes": "Check the audit trail records every stage of the trade; confirm the P&L reflects the fill."},
]


def test_a_check_of_the_outcome_keeps_waiting_on_the_keys_and_code_does_not():
    tasks = copy.deepcopy(PLAN)
    engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert by["T2"]["type"] == "decision" and by["T2"]["tool"] == "LLM"
    assert "T2" not in by["T3"]["depends_on"] and "T1" in by["T3"]["depends_on"]      # code: unhooked
    assert "T2" in by["T4"]["depends_on"]                                             # the check: still waits
    assert engine_plan._checks_the_outcome({"tool": "LLM", "name": "Confirm the P&L reflects the fill"})
    assert not engine_plan._checks_the_outcome({"tool": "Shell", "name": "Verify the batch ran"})   # becomes a test
    assert not engine_plan._checks_the_outcome({"tool": "LLM", "name": "Summarize the lab"})


def test_a_plan_edit_carries_the_node_type_and_the_editor_takes_it():
    assert "node_type" in node_editor.EDITABLE_FIELDS and "node_type" in node_editor.INVALIDATING_FIELDS
    assert node_editor.NODE_TYPES == {"task", "decision", "parallel_group", "checkpoint"}
    src = inspect.getsource(engine_plan.apply_to_job)
    assert 'fields["node_type"] = _nt' in src
    assert src.index('fields["node_type"] = _nt') < src.index("if not fields:")


async def test_the_editor_refuses_a_node_type_outside_the_vocabulary():
    from unittest.mock import AsyncMock, patch

    node = {"node_key": "T2", "status": "pending", "depends_on": [], "edit_version": 1}
    with patch.object(node_editor, "_load_nodes", AsyncMock(return_value=[node])):
        out = await node_editor.edit_node("job", "T2", {"node_type": "question"}, db=AsyncMock())
    assert out == {"error": "node_type must be one of ['checkpoint', 'decision', 'parallel_group', 'task']",
                   "http_status": 400}
