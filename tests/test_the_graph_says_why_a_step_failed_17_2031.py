"""§17.2031 — the plan graph says why a failed step failed, and offers the verbs that move it.

§17.1572 parked it (2026-10-15): a job that went `blocked` on a step's third failed retry showed on the graph -- where an
auto run is watched (§17.1513) -- as a red box and nothing else. The why was in `dag_nodes.last_verification_reason`;
`GET /nodes/{job_id}` (the graph's read) never carried it, and the drawer had a panel only for a parked step. The read
now carries `failure_reason` on a failed step (the name `/exec/status` gives it, §17.450); `plan.js` renders it with
Retry / Edit / Skip (pinned by tests/ui/plan_failed_step.test.mjs).
"""
from __future__ import annotations

import pathlib
import uuid

import pytest
from sqlalchemy import text

from app.modules import node_editor

WHY = "verifier 0.31: the sandbox ran 18 tests, 4 failed -- MagicMock can't be used in 'await' expression"


async def _drop(jid: str):
    from app.database import async_session
    async with async_session() as db:
        await db.execute(text("DELETE FROM dag_nodes WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
        await db.commit()


@pytest.mark.integration
@pytest.mark.timeout(900)
async def test_the_graphs_read_carries_a_failed_steps_reason_and_only_a_failed_ones():
    from app.database import async_session
    jid = str(uuid.uuid4())
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'lab', 'blocked')"), {"id": jid})
            # T5 failed with a reason; T2 failed with none; T3 is done but kept an old verifier note from a retry
            for k, st, why, deps, order in (("T1", "done", None, [], 1), ("T2", "failed", None, ["T1"], 2),
                                            ("T3", "done", "retry 1: 0.4, kept", ["T1"], 3),
                                            ("T5", "failed", WHY, ["T1"], 5), ("T6", "pending", None, ["T5"], 6)):
                await db.execute(text(
                    "INSERT INTO dag_nodes (job_id, node_key, title, status, tool, prompt_template, depends_on, "
                    "execution_order, last_verification_reason) VALUES (:j, :k, :k, :s, 'LLM', 'p', :d, :o, :w)"),
                    {"j": jid, "k": k, "s": st, "d": deps, "o": order, "w": why})
            await db.commit()
            out = await node_editor.list_nodes(jid, db)
        by = {n["node_key"]: n for n in out["nodes"]}
        assert out["job_status"] == "blocked"
        assert by["T5"]["failure_reason"] == WHY
        assert by["T2"]["failure_reason"] is None
        assert by["T3"]["failure_reason"] is None, "a done step's old verifier note is not a failure reason"
        assert by["T6"]["failure_reason"] is None and by["T1"]["failure_reason"] is None
    finally:
        await _drop(jid)


def test_the_graph_renders_the_panel_and_opens_on_the_stopping_step():
    """The DOM half of plan.js the node test cannot reach: the drawer's read view mounts the failed panel, the first
    load selects the stopping step, and the verbs call the endpoints the Run stage uses (/exec/retry, /skip)."""
    src = (pathlib.Path(__file__).parents[1] / "app/ui/static/views/plan.js").read_text()
    read_view = src[src.index("function readView(node)"):]
    assert "failedPanel(node)," in read_view[:400]
    assert "selectedKey = stoppedOnKey(nodes, jobStatus)" in src
    panel = src[src.index("function failedPanel(node)"):src.index("function readView(node)")]
    assert "failedStepModel(node, jobStatus, nodes)" in panel and "api.post(v.endpoint" in panel
    assert 'sessionStorage.setItem("scaffold_autorun", jobId)' in panel
