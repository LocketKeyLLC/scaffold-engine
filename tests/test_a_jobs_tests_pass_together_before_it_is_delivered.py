"""§17.1933 — just before the delivery question is parked, the job's tests run together (as the engine's CI runs
them); a failing file whose writer step writes tests only is read again (once), the run recorded as that step's own
sandbox attempt so its failing tests are not kept and its passing ones are; the delivery waits on it.

Live (follow-up 5b4880c0, 2026-11-05 14:15 UTC): every step done, the delivered branch ran 12 failing tests -- all in
T5's `tests/test_paper_twelve_modules.py`, written for modules seven later fix steps rewrote."""
from __future__ import annotations

import inspect

from app.modules import execution_agent as ea
from app.modules import lab_together as lt
from app.modules.engine_plan import sandbox_counts
from app.sandbox.engine_tests import failed_tests

STDOUT = """PASSED tests/test_paper_x.py::test_ok
FAILED tests/test_paper_twelve_modules.py::TestPaperOrderExecutor::test_execute_order_submits_and_records - KeyError
FAILED tests/test_paper_twelve_modules.py::test_fetch_disclosures_basic - AssertionError: assert 0 >= 1
ERROR tests/test_paper_y.py::test_setup - fixture 'db' not found
FAILED tests/test_paper_twelve_modules.py::test_fetch_disclosures_basic - AssertionError: assert 0 >= 1
"""


def test_the_failing_and_erroring_tests_are_read_from_the_summary():
    assert failed_tests(STDOUT) == ["tests/test_paper_twelve_modules.py::TestPaperOrderExecutor::test_execute_order_submits_and_records",
                                    "tests/test_paper_twelve_modules.py::test_fetch_disclosures_basic",
                                    "tests/test_paper_y.py::test_setup"]


def _row(key: str, files: dict[str, str], **kw) -> dict:
    return {"node_key": key, "status": "done", "title": key,
            "output_text": "\n\n".join(f"```python\n# {p}\n{c}\n```" for p, c in files.items()), **kw}


def test_only_a_tests_only_step_of_this_job_is_read_again_and_once():
    tests = {"tests/test_paper_twelve_modules.py": ("def test_a():\n    pass\n", "T5"),
             "tests/test_paper_y.py": ("def test_setup(db):\n    pass\n", "T6"),
             "tests/test_paper_z.py": ("def test_z():\n    pass\n", "f00ea97f:T22")}
    by_key = {"T5": _row("T5", {"tests/test_paper_twelve_modules.py": "def test_a():\n    pass"}),
              "T6": _row("T6", {"app/modules/paper_y.py": "X = 1", "tests/test_paper_y.py": "def test_setup(db):\n    pass"}),
              "f00ea97f:T22": _row("f00ea97f:T22", {"tests/test_paper_z.py": "def test_z():\n    pass"})}
    failed = failed_tests(STDOUT) + ["tests/test_paper_z.py::test_z"]
    got = lt.steps_to_read_again(tests, failed, by_key)
    assert got == [("T5", "tests/test_paper_twelve_modules.py",
                    ["test_execute_order_submits_and_records", "test_fetch_disclosures_basic"])]
    by_key["T5"]["tool_config"] = {lt.RERAN_KEY: ["test_fetch_disclosures_basic"]}
    assert lt.steps_to_read_again(tests, failed, by_key) == []


def test_the_record_is_a_sandbox_verdict_and_the_hooks_are_in_place():
    assert sandbox_counts("§17.1933 run with the job's tests before delivery (12 failed, 376 passed in 100.92s)") == (12, 376)
    assert sandbox_counts("§17.1933 run with the job's tests before delivery (2 failed, 0 passed in 0.0s)") == (2, 0)
    assert "stale_test_steps(job_id" in inspect.getsource(ea._pause_for_decision)
    assert "take_reset(job_id)" in inspect.getsource(ea._run_parallel_frontier)
    lt._JUST_RESET.add("j")
    assert lt.take_reset("j") is True and lt.take_reset("j") is False


def _deliver():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("deliver_job", Path(__file__).parent.parent / "scripts" / "deliver_job.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_delivered_labs_tests_go_under_their_own_folder_with_its_tables_emptied():
    """§17.1932 — the engine's CI runs every test in one session: the lab's tests carry the sandbox's guarantee."""
    import ast
    dj = _deliver()
    assert dj.lab_dir(["paper_trade_proposer", "paper_fill_tracker"], "paper_wire_engine_entry_module") == "paper"
    assert dj.lab_dir(["paper_a", "lab_b"], "paper_wire_x") == "paper_wire_x"
    assert dj.place("tests/test_paper_x.py", "paper") == "tests/labs/paper/test_paper_x.py"
    assert dj.place("app/modules/paper_x.py", "paper") == "app/modules/paper_x.py"
    src = dj._CONFTEST.format(lab="paper", tables=["signal", "pnl"])
    ast.parse(src)
    assert "TABLES = ['signal', 'pnl']" in src and "RESTART IDENTITY CASCADE" in src and "autouse=True" in src


# ── §17.1934 — a stale tests-only step is held to the modules no current test file covers ──────────────────────

FILES = {"app/modules/paper_a.py": ("A = 1\n", "F1"), "tests/test_paper_a.py": ("def test_a():\n    pass\n", "F1"),
         "app/modules/paper_b.py": ("B = 1\n", "T9"), "tests/test_paper_b.py": ("def test_b():\n    pass\n", "T3"),
         "app/modules/paper_c.py": ("C = 1\n", "f00ea97f:T5")}
DRAFT = ("```python\n# tests/test_paper_twelve_modules.py\nfrom app.modules.paper_a import A\n"
         "from app.modules.paper_b import B\nfrom app.modules.paper_c import C\nfrom app.config import settings\n```")


def test_the_modules_a_current_test_file_covers_are_left_to_it():
    assert lt.uncovered_modules(DRAFT, FILES) == (["paper_b", "paper_c"], ["paper_a"])   # b's tests are older than it
    notes, left = lt.narrowed_notes("Python test module `tests/test_paper_twelve_modules.py`.", DRAFT, FILES)
    assert left == ["paper_b", "paper_c"] and "write this file's tests for `paper_b`, `paper_c` only" in notes
    assert lt.narrowed_notes("x", DRAFT.replace("from app.modules.paper_a import A\n", ""), FILES) is None   # none covered


def test_the_plan_pass_narrows_a_stale_step_once():
    tasks = [{"id": "T5", "name": "Write tests for twelve modules", "status": "failed", "notes": "Python test module.",
              "tool_config": {lt.RERAN_KEY: ["test_x"]}},
             {"id": "T6", "name": "Other", "status": "failed", "notes": "n", "tool_config": {}}]
    rows = [{"node_key": "T5", "output_text": DRAFT}, {"node_key": "T6", "output_text": DRAFT}]
    assert lt.narrow_stale_test_steps(tasks, rows, FILES) == ["stale_tests_narrowed: T5 writes tests for paper_b, paper_c only"]
    assert tasks[0]["tool_config"][lt.NARROWED_KEY] == ["paper_b", "paper_c"] and "§17.1934" in tasks[0]["notes"]
    assert lt.narrow_stale_test_steps(tasks, rows, FILES) == []                         # once; T6 was never read again
