"""§17.1942 — before delivery, a test file the linter refuses, or one that fails with the job's tests while its writer
also writes a module, gets a tests-only fix step the delivery waits on. Both were only logged (5b4880c0, 2026-11-05)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import engine_source
from app.modules import lab_together as lt


def _row(key: str, files: dict[str, str]) -> dict:
    out = "\n\n".join(f"```python\n# {p}\n{c}\n```" for p, c in files.items())
    return {"node_key": key, "title": key, "output_text": out, "tool_config": {}}


class _Session:
    def __init__(self, rows=()):
        self.db = MagicMock()
        self.db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=list(rows))))

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc):
        return False


def test_a_module_writers_failing_file_is_handed_to_a_fix_step_not_logged():
    tests = {"tests/test_paper_t.py": ("def test_a():\n    pass\n", "T5"),
             "tests/test_paper_y.py": ("def test_y():\n    pass\n", "T6"),
             "tests/test_paper_z.py": ("def test_z():\n    pass\n", "f00ea97f:T22")}
    by_key = {"T5": _row("T5", {"tests/test_paper_t.py": "def test_a():\n    pass"}),
              "T6": _row("T6", {"app/modules/paper_y.py": "X = 1", "tests/test_paper_y.py": "def test_y():\n    pass"}),
              "f00ea97f:T22": _row("f00ea97f:T22", {"tests/test_paper_z.py": "def test_z():\n    pass"})}
    failed = ["tests/test_paper_t.py::test_a", "tests/test_paper_y.py::test_y", "tests/test_paper_z.py::test_z"]
    owned: list = []
    assert lt.steps_to_read_again(tests, failed, by_key, owned) == [("T5", "tests/test_paper_t.py", ["test_a"])]
    assert owned == [("T6", "tests/test_paper_y.py", ["test_y"])]          # the earlier job's file stays logged


@pytest.mark.asyncio
async def test_the_fix_step_is_tests_only_and_added_once(monkeypatch):
    insert = AsyncMock(return_value="DELIVERL1")
    monkeypatch.setattr(lt, "_insert_fix_step", insert)
    monkeypatch.setattr(lt, "async_session", lambda: _Session([("T6", "Write paper_y")]))
    got = await lt.failing_test_fix_steps("j", "DELIVER", [("T6", "tests/test_paper_y.py", ["test_y"])], "1 failed")
    assert got == ["DELIVERL1"]
    _job, _deliver, _taken, title, prompt = insert.await_args.args
    assert title == "Fix test_paper_y (its tests fail with the job's)"
    assert prompt.startswith("Python test module `tests/test_paper_y.py` -- step T6's tests") and "`test_y`" in prompt
    assert "Change the tests only" in prompt
    monkeypatch.setattr(lt, "async_session", lambda: _Session([("DELIVERL1", title)]))
    insert.reset_mock()
    assert await lt.failing_test_fix_steps("j", "DELIVER", [("T6", "tests/test_paper_y.py", ["test_y"])], "") == []
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_test_files_lint_findings_get_a_tests_only_fix_step(monkeypatch):
    files = {"tests/test_paper_a.py": ("from app.modules.paper_a import *\n", "T3"),
             "app/modules/paper_a.py": ("A = 1\n", "T3")}
    monkeypatch.setattr(engine_source, "code_rows", AsyncMock(return_value=[{"node_key": "T3"}]))
    monkeypatch.setattr(engine_source, "job_files", lambda _rows: files)
    monkeypatch.setattr(engine_source, "lint_findings", AsyncMock(return_value=[
        "`tests/test_paper_a.py:4` ruff F821: Undefined name `x` -- fix it"]))
    monkeypatch.setattr(engine_source, "tool_audit_findings", AsyncMock(return_value=[]))
    monkeypatch.setattr(lt, "async_session", lambda: _Session([]))
    insert = AsyncMock(return_value="DELIVERL1")
    monkeypatch.setattr(lt, "_insert_fix_step", insert)
    assert await lt.lint_fix_steps("j", "DELIVER") == ["DELIVERL1"]
    _job, _deliver, _taken, title, prompt = insert.await_args.args
    assert title == "Fix test_paper_a (the repo's linter)"
    assert prompt.startswith("Python test module `tests/test_paper_a.py` -- step T3's tests") and "F821" in prompt
