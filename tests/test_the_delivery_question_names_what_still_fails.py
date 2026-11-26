"""§17.2026 — what the checks before delivery leave open is in the delivery question, not only in the log.

§17.1933: "A file failing again, or one a module-writing step owns, is logged and the question is parked as before
(PARKED)". §17.1942 gave the module writer's file a fix step, once per file; a file that fails together again after
its one read-again or its one fix step, or the earlier job's file no step of this job may redraw, was still only a
`lab_tests_together_failing` log line -- and the operator was asked "Is this job's code merged into the engine and
running?" about code whose tests fail together. The same for a together-run the sandbox never ran (off, unreachable,
another engine): the question opened as if it had passed.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import engine_source
from app.modules import execution_agent as ea
from app.modules import lab_together as lt
from app.sandbox import engine_tests as et


def _row(key: str, files: dict[str, str]) -> dict:
    return {"node_key": key, "status": "done", "title": key,
            "output_text": "\n\n".join(f"```python\n# {p}\n{c}\n```" for p, c in files.items())}


class _Session:
    def __init__(self, rows=()):
        self.db = MagicMock()
        self.db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=list(rows))))

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc):
        return False


T5 = {"tests/test_paper_twelve_modules.py": "def test_a():\n    pass\n\ndef test_b():\n    pass"}
T22 = {"tests/test_paper_z.py": "def test_z():\n    pass"}
FAILED = ["tests/test_paper_twelve_modules.py::test_a", "tests/test_paper_twelve_modules.py::test_b[2]",
          "tests/test_paper_twelve_modules.py::test_b[3]", "tests/test_paper_z.py::test_z"]


@pytest.fixture
def together(monkeypatch):
    rows = [_row("T5", T5), _row("f00ea97f:T22", T22)]
    monkeypatch.setattr(engine_source, "code_rows", AsyncMock(return_value=rows))
    check = AsyncMock(return_value=et.EngineTestResult(
        "fail", "§17.1765 … 3 failed, 4 passed in 2.10s", [], passed=[], failed=FAILED))
    monkeypatch.setattr(et, "check", check)
    reset = AsyncMock()
    monkeypatch.setattr("app.modules.node_editor._reset_keys", reset)
    lt.clear_still_open("j")
    return check, reset


@pytest.mark.asyncio
async def test_a_file_failing_again_after_its_one_read_again_is_named_for_the_question(monkeypatch, together):
    _check, reset = together
    # T5 was read again for these tests once already (its mark), the earlier job's file is no step's to redraw
    monkeypatch.setattr(lt, "async_session", lambda: _Session([("T5", {lt.RERAN_KEY: ["test_a"]})]))
    assert await lt.stale_test_steps("j", "DELIVER") == []
    reset.assert_not_awaited()
    note = lt.still_open_note("j")
    assert ("- `tests/test_paper_twelve_modules.py` (step T5): 2 test(s) fail when the job's tests run together "
            "(3 failed, 4 passed in 2.10s): test_a, test_b -- its step was read again for this once already, or is "
            "not this job's to redraw\n") in note
    assert "- `tests/test_paper_z.py` (step f00ea97f:T22): 1 test(s) fail" in note


@pytest.mark.asyncio
async def test_a_file_failing_again_after_its_fix_step_is_named_for_the_question(monkeypatch):
    insert = AsyncMock(return_value="DELIVERL2")
    monkeypatch.setattr(lt, "_insert_fix_step", insert)
    title = "Fix test_paper_y (its tests fail with the job's)"
    monkeypatch.setattr(lt, "async_session", lambda: _Session([("DELIVERL1", title)]))
    lt.clear_still_open("j")
    files = [("T6", "tests/test_paper_y.py", ["test_y", "test_w"]), ("T7", "tests/test_paper_v.py", ["test_v"])]
    assert await lt.failing_test_fix_steps("j", "DELIVER", files, "3 failed") == ["DELIVERL2"]
    assert insert.await_args.args[3] == "Fix test_paper_v (its tests fail with the job's)"
    assert lt.still_open_note("j").endswith(
        "- `tests/test_paper_y.py` (step T6): 2 test(s) fail when the job's tests run together (3 failed): test_y, "
        "test_w -- its fix step \"Fix test_paper_y (its tests fail with the job's)\" ran once already\n")


@pytest.mark.asyncio
async def test_a_together_run_that_never_ran_is_named_and_one_that_passed_is_not(monkeypatch, together):
    check, _reset = together
    monkeypatch.setattr(lt, "async_session", lambda: _Session([]))
    check.return_value = et.EngineTestResult("skip", "engine-test sandbox holds another engine (a vs b)", [])
    assert await lt.stale_test_steps("j", "DELIVER") == []
    assert lt.still_open_note("j").endswith(
        "- the job's tests were not run together (skip): engine-test sandbox holds another engine (a vs b)\n")
    lt.clear_still_open("j")
    check.return_value = et.EngineTestResult("pass", "tests passed", [])
    assert await lt.stale_test_steps("j", "DELIVER") == []
    assert lt.still_open_note("j") == ""


def test_the_note_lists_each_line_once_and_clears():
    lt.clear_still_open("k")
    lt.note_still_open("k", "a")
    lt.note_still_open("k", "a")
    lt.note_still_open("k", "b")
    assert lt.still_open_note("k").endswith(":\n- a\n- b\n")
    lt.clear_still_open("k")
    assert lt.still_open_note("k") == ""


def test_the_delivery_question_carries_the_note():
    src = inspect.getsource(ea._pause_for_decision)
    assert src.index("clear_still_open(job_id)") < src.index("review_fix_steps(job_id)")
    assert "_still = still_open_note(job_id)" in src
    assert 'frame["detail"] = (str(frame.get("detail") or "").rstrip() + "\\n\\n" + _still).strip()' in src
