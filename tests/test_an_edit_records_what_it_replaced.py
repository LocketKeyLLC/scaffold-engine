"""§17.1795 — an edit's ledger entry records the real values it replaced; signature refusals are not omissions.

Live (job f00ea97f T25, the integration step): the §17.1793 split fired on "is missing required argument(s)" (a
call-shape refusal) and turned a wiring step into a "data" half; undoing it from dag_node_edits was impossible -- the
pre-image had `prompt_template: null` (edit_node read it from `_load_nodes`, which carries no prompt).
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import node_editor
from app.modules.engine_plan import _OMISSION_RE

text = node_editor.text


@pytest.mark.integration
async def test_an_edit_records_the_text_it_replaced():
    from app.database import async_session
    jid = str(uuid.uuid4())
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'x', 'running')"), {"id": jid})
            await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, tool, execution_order, "
                "tool_config) VALUES (:j, 'T25', 'Integrate', 'pending', 'the original text', 'CodeGen', 0, "
                "CAST(:c AS jsonb))"), {"j": jid, "c": json.dumps({"a": 1})})
            await db.commit()
        async with async_session() as db:
            out = await node_editor.edit_node(jid, "T25", {"prompt_template": "new text", "tool_config": {"b": 2}},
                                              edited_by="test", db=db)
        assert out["status"] == "ok"
        async with async_session() as db:
            before = (await db.execute(text(
                "SELECT before FROM dag_node_edits WHERE job_id = :j AND node_key = 'T25' AND op = 'edit'"),
                {"j": jid})).scalar()
        before = before if isinstance(before, dict) else json.loads(before)
        assert before == {"prompt_template": "the original text", "tool_config": {"a": 1}}
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


@pytest.mark.parametrize("reason,omits", [
    ("`generate_signals(…)` is missing required argument(s) `timestamp` -- the signature is …", False),   # T25
    ("`write_pnl(…)` is missing required argument(s) `as_of_ts`", False),
    ("The step's headline export render_dashboard() (HTML/CLI view) is entirely absent", True),           # T24
    ("the required tests/test_paper_operator_dashboard.py was not written at all", True),
    ("missing the explicitly required render_dashboard() export", True),
])
def test_only_a_task_part_left_out_counts_as_an_omission(reason, omits):
    assert bool(_OMISSION_RE.search(reason)) is omits
