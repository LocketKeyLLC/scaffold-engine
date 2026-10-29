"""§17.1864 — a retry keeps every test.

Live (follow-up 5b4880c0 T5, 2026-10-29, the §17.1863 round): the §17.1859 redraws kept every test on all four
attempts (23, 23, 23; 11, 11; 6, 6; 5, 5). Each RETRY dropped them: 23 tests, then 11 (13 of the 23 gone, the ones the
sandbox had passed among them), 6, 5. A smaller file passes more easily and tests less; nothing the engine showed or
checked held the next attempt to what the last one wrote."""
from __future__ import annotations

import inspect
import json
import uuid

import pytest
import sqlalchemy as sa

from app.modules import engine_source as es
from app.modules import execution_agent as xa
from app.modules.engine_contracts import dropped_tests, written_tests

ATTEMPT_1 = '''```python
# app/modules/paper_risk_control_module.py
def check(x):
    return x
```

```python
# tests/test_paper_twelve_modules.py
import pytest
from app.modules.paper_risk_control_module import check


def test_daily_loss_limit_halts_new_orders():
    assert check(1) == 1


class TestApprovalGateRefusal:
    async def test_approval_gate_refuses_unapproved_orders(self):
        assert check(2) == 2

    def helper(self):
        return 3


@pytest.mark.asyncio
async def test_kill_switch_cancels_orders_and_closes_positions():
    assert check(3) == 3
```
'''
ATTEMPT_2 = '''```python
# tests/test_paper_twelve_modules.py
from app.modules.paper_risk_control_module import check


def test_daily_loss_limit_halts_new_orders():
    assert check(1) == 1


def test_something_new():
    assert check(4) == 4
```
'''


def test_the_tests_of_an_output_are_read_by_file_in_source_order():
    assert written_tests(ATTEMPT_1) == {"tests/test_paper_twelve_modules.py": [
        "test_daily_loss_limit_halts_new_orders", "test_approval_gate_refuses_unapproved_orders",
        "test_kill_switch_cancels_orders_and_closes_positions"]}
    assert written_tests("```python\n# app/modules/x.py\ndef test_me(): pass\n```") == {}      # not a test file
    assert written_tests("```python\n# tests/test_x.py\ndef test_a(:\n```") == {}             # does not parse
    assert written_tests("") == {}


def test_a_retry_that_drops_tests_is_refused_by_name_and_one_that_keeps_them_is_not():
    prior = written_tests(ATTEMPT_1)
    assert dropped_tests(ATTEMPT_2, prior) == [
        "`tests/test_paper_twelve_modules.py` dropped 2 of the 3 tests your earlier attempts wrote: "
        "`test_approval_gate_refuses_unapproved_orders`, `test_kill_switch_cancels_orders_and_closes_positions` "
        "-- keep every test by name; fix the ones the sandbox named, never drop them"]
    assert dropped_tests(ATTEMPT_2, prior)[0].startswith("`tests/")          # §17.1859: a redraw, not the attempt
    assert dropped_tests(ATTEMPT_1, prior) == []                            # every test kept (a new one is fine)
    assert dropped_tests(ATTEMPT_2, {}) == [] and dropped_tests(ATTEMPT_2, None) == []     # a first attempt
    # the whole file gone counts every test of it
    assert dropped_tests("```python\n# app/modules/paper_risk_control_module.py\nX = 1\n```", prior)[0].startswith(
        "`tests/test_paper_twelve_modules.py` dropped 3 of the 3 tests")


def test_the_engine_block_lists_the_tests_a_retry_must_keep():
    node = {"title": "Write tests for twelve modules", "prompt_template": "Python test module `tests/test_paper_twelve_modules.py`",
            "prior_tests": {"tests/test_paper_twelve_modules.py": ["test_a", "test_b"]}}
    b = es.block_for(node, set())
    assert ("### Tests your earlier attempts wrote (keep every one, by name -- fix the ones the sandbox named, never "
            "drop them)\n    tests/test_paper_twelve_modules.py: test_a, test_b\n") in b
    assert b.index("Tests your earlier attempts wrote") < b.index("### Conventions")       # near the top: survives the cut
    assert "Tests your earlier attempts wrote" not in es.block_for({**node, "prior_tests": {}}, set())
    assert "Tests your earlier attempts wrote" not in es.block_for({k: v for k, v in node.items() if k != "prior_tests"}, set())


def test_the_executor_records_tests_at_a_refusal_reads_them_at_a_retry_and_the_gate_asks_for_them():
    src = inspect.getsource(xa)
    assert '"tests": _ec_written_tests(str(output or ""))' in src
    assert 'node_snapshot["prior_tests"] = await prior_tests(db, job_id, str(node_id))' in src
    assert xa._ec_written_tests(ATTEMPT_1) == written_tests(ATTEMPT_1) and xa._ec_written_tests(None) == {}      # type: ignore[arg-type]
    g = inspect.getsource(es.engine_findings)
    assert 'dropped_tests(output, await prior_tests(db, job_id, str(me["node_id"])))' in g


@pytest.mark.integration
async def test_prior_tests_are_read_from_the_run_log_over_the_same_window_as_refusals():
    from app.database import async_session
    text = sa.text
    jid, nid = str(uuid.uuid4()), None
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'tests history', 'running')"),
                             {"id": jid})
            nid = (await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, execution_order) "
                "VALUES (:j, 'T5', 'Write tests', 'pending', 0) RETURNING id"), {"j": jid})).scalar()
            for msg, details in [
                    ("Node 'Write tests' -> failed", {"reason": "r1", "tests": {"tests/test_a.py": ["test_x", "test_y"]}}),
                    ("Node 'Write tests' evidence: unsupported=0", {"tests": {"tests/test_a.py": ["test_not_counted"]}}),
                    ("Node 'Write tests' -> failed", {"reason": "r2", "tests": {"tests/test_a.py": ["test_y", "test_z"],
                                                                              "tests/test_b.py": ["test_q"]}}),
                    ("Node 'Write tests' -> failed", {"reason": "r3"})]:
                await db.execute(text("INSERT INTO execution_logs (job_id, node_id, log_level, message, details) "
                                      "VALUES (:j, :n, 'warning', :m, CAST(:d AS jsonb))"),
                                 {"j": jid, "n": nid, "m": msg, "d": json.dumps(details)})
            await db.commit()
        async with async_session() as db:
            got = await xa.prior_tests(db, jid, str(nid))
        assert got == {"tests/test_a.py": ["test_x", "test_y", "test_z"], "tests/test_b.py": ["test_q"]}
        async with async_session() as db:          # the gate reads the same window, through the node's key
            found = await es.engine_findings(db, jid, "T5", "```python\n# tests/test_a.py\ndef test_x(): pass\n```")
        assert any(f.startswith("`tests/test_a.py` dropped 2 of the 3 tests") for f in found)
        assert any(f.startswith("`tests/test_b.py` dropped 1 of the 1 tests") for f in found)
        async with async_session() as db:          # the step's prompt edited: earlier attempts no longer count
            await db.execute(text("INSERT INTO dag_node_edits (job_id, node_key, op, after, edited_by) VALUES "
                                  "(:j, 'T5', 'edit', CAST(:a AS jsonb), 'operator')"),
                             {"j": jid, "a": json.dumps({"prompt_template": "new"})})
            await db.commit()
        async with async_session() as db:
            assert await xa.prior_tests(db, jid, str(nid)) == {}
    finally:
        async with async_session() as db:
            for tbl in ("execution_logs", "dag_node_edits"):
                await db.execute(text(f"DELETE FROM {tbl} WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
