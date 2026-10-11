"""§17.1461 — a parallel worker never does (or skips) the operator's part.

Live (2026-10-11 01:25:26, 20 s after §17.1458 shipped): while the pause was drafting ADD151, a parallel worker
claimed ADD153 ("Your devices: join the tailnet…", tool human) and the H3 short-circuit recorded it done —
"Skipped: human review not required in auto mode". ADD152 (the router, tool LLM, wholly the operator's) would have
been "completed" as prose the same way.
"""
import asyncio
import json
import pathlib

from app.modules import execution_agent as ea
from app.modules import operator_steps as ops

FIX = pathlib.Path("tests/fixtures")
ADD152 = json.loads((FIX / "add152_node_2026_10_10.json").read_text())
ADD153 = json.loads((FIX / "add153_node_2026_10_11.json").read_text())
ADD151 = json.loads((FIX / "add151_node_2026_10_10.json").read_text())


class _DB:
    def __init__(self, decisions=None):
        self.decisions, self.sql = decisions or {}, []

    async def execute(self, stmt, params=None):
        q = str(stmt)
        self.sql.append(q)
        d = self.decisions

        class R:
            def scalar(self_inner):
                return json.dumps(d)
        return R()

    async def commit(self):
        pass


def _back(node, decisions=None):
    db = _DB(decisions)
    return asyncio.run(ea._hand_back_for_operator(db, "j", {**node, "id": "x"}, node.get("tool", "LLM"))), db


def test_the_live_human_step_is_handed_back():
    res, db = _back(ADD153)
    assert res == {"status": "needs_approval", "node_key": "ADD153", "title": ADD153["title"], "tool": "human"}
    assert any("SET status = 'pending'" in q for q in db.sql)


def test_a_wholly_operator_llm_step_is_handed_back_too():
    res, _ = _back(ADD152)
    assert res and res["status"] == "needs_approval"


def test_the_engines_step_runs_and_a_half_done_one_is_held():
    assert _back(ADD151)[0] is None
    res, _ = _back(ADD151, {"ADD151": {"result": ops.ENGINE_HALF_DONE}})
    assert res and res["status"] == "needs_approval"


def test_it_sits_before_the_human_skip():
    src = pathlib.Path("app/modules/execution_agent.py").read_text()
    assert src.index("_op_held = await _hand_back_for_operator(db, job_id, node, tool)") \
        < src.index("Skipped: human review not required in auto mode")


def test_an_optional_review_is_still_skippable():
    node = {"node_key": "R1", "title": "Review the plan", "tool": "human_review", "description": "Look it over."}
    assert ops.is_wholly_operators(node) is False and _back(node)[0] is None
