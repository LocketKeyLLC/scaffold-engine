"""§17.1493 — resetting a step drops its "the rest is the operator's" mark (live ADD169)."""
import asyncio

from app.modules import node_editor
from app.modules.operator_steps import ENGINE_HALF_DONE


class _DB:
    def __init__(self, decided):
        self.decided, self.dropped = decided, []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "#-" in sql:
            self.dropped.append(params["k"])
        decided = self.decided

        class R:
            def scalar(self_inner):
                return decided
        return R()


def test_a_reset_drops_only_the_engine_half_mark():
    db = _DB({"ADD169": {"result": ENGINE_HALF_DONE, "at": "x"},
              "ADD161": {"result": "ran", "at": "x"}})
    gone = asyncio.run(node_editor._drop_engine_half_marks(db, "j", ["ADD169", "ADD161"]))
    assert gone == ["ADD169"] and db.dropped == ["ADD169"]


def test_no_decisions_is_a_no_op():
    db = _DB(None)
    assert asyncio.run(node_editor._drop_engine_half_marks(db, "j", ["ADD169"])) == []


def test_reset_node_calls_it():
    import inspect
    src = inspect.getsource(node_editor.reset_node)
    assert "_drop_engine_half_marks(db, job_id, reset_keys)" in src
