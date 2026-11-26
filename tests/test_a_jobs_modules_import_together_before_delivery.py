"""§17.2027 — before the delivery question, every one of the job's modules is imported in the sandbox with the rest
present, each on its own; a module that does not import gets a fix step for the file the failure is in.

Live (follow-up 5b4880c0, 2026-11-05, §17.1931): API_ALPACA_AUDITF6 rewrote `paper_trade_proposer` without
`MAX_PROPOSALS_PER_BATCH`; the backtester imported it, the backtesting entry point and the wiring module imported the
backtester, and the delivery question opened -- the delivered branch's tests did not collect. §17.1931 gates a step's
rewrite; the together-run (§17.1933) imports a module only when one of the job's test files does. Nothing imported the
job's modules together before delivery (parked in §17.1931).
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import engine_source
from app.modules import execution_agent as ea
from app.modules import lab_together as lt
from app.sandbox import engine_tests as et

#: the 5b4880c0 shape: a rewrite dropped a constant, its importer and two modules above it no longer import
MODULES = {
    "app/modules/paper_trade_proposer.py": "def propose(signals):\n    return list(signals)\n",
    "app/modules/paper_walk_forward_backtester.py": (
        "from app.modules.paper_trade_proposer import MAX_PROPOSALS_PER_BATCH, propose\n\n"
        "def run(signals):\n    return propose(signals)[:MAX_PROPOSALS_PER_BATCH]\n"),
    "app/modules/paper_backtesting_entry_point.py": (
        "from app.modules.paper_walk_forward_backtester import run\n\ndef entry():\n    return run([])\n"),
    "app/modules/paper_wire_engine_entry_module.py": (
        "from app.modules import paper_backtesting_entry_point\n\ndef install(app):\n    return None\n"),
    "app/modules/paper_broken_syntax.py": "def f(:\n    pass\n",
    "app/modules/paper_fine.py": "X = 1\n",
}


def _run_import_script(tmp_path, mods: list[str]) -> subprocess.CompletedProcess:
    root = tmp_path / "run"
    for p, code in {"app/__init__.py": "", "app/modules/__init__.py": "", **MODULES}.items():
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_text(code)
    return subprocess.run([sys.executable, "-c", et._IMPORT_SCRIPT.format(mods=mods)], cwd=root,
                          capture_output=True, text=True, timeout=60)


def test_the_import_run_imports_every_module_and_names_each_that_fails_with_its_frames(tmp_path):
    mods = sorted(p[:-3].replace("/", ".") for p in MODULES)
    res = _run_import_script(tmp_path, mods)
    assert res.returncode == 1, res.stdout + res.stderr
    got = {f["module"]: f for f in et.import_failures(res.stdout)}
    assert sorted(got) == ["app.modules.paper_backtesting_entry_point", "app.modules.paper_broken_syntax",
                           "app.modules.paper_walk_forward_backtester", "app.modules.paper_wire_engine_entry_module"]
    assert got["app.modules.paper_walk_forward_backtester"]["frames"][-1] == "app/modules/paper_walk_forward_backtester.py:1"
    assert "cannot import name 'MAX_PROPOSALS_PER_BATCH'" in got["app.modules.paper_walk_forward_backtester"]["error"]
    # the modules above it fail in the backtester's frame (it is imported again inside them)
    assert got["app.modules.paper_wire_engine_entry_module"]["frames"][-1] \
        == "app/modules/paper_walk_forward_backtester.py:1"
    assert got["app.modules.paper_broken_syntax"]["frames"][-1] == "app/modules/paper_broken_syntax.py:1"
    assert got["app.modules.paper_broken_syntax"]["error"].startswith("SyntaxError")
    assert "imported 2 of 6" in res.stdout
    ok = _run_import_script(tmp_path, ["app.modules.paper_fine", "app.modules.paper_trade_proposer"])
    assert ok.returncode == 0 and et.import_failures(ok.stdout) == [] and "imported 2 of 2" in ok.stdout


class _Session:
    def __init__(self, rows=()):
        self.db = MagicMock()
        self.db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=list(rows))))

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc):
        return False


def _failures() -> list[dict]:
    err = ("ImportError: cannot import name 'MAX_PROPOSALS_PER_BATCH' from 'app.modules.paper_trade_proposer' "
           "(/run/app/modules/paper_trade_proposer.py)")
    return [
        {"module": "app.modules.paper_backtesting_entry_point", "error": err,
         "frames": ["app/modules/paper_backtesting_entry_point.py:1", "app/modules/paper_walk_forward_backtester.py:1"]},
        {"module": "app.modules.paper_broken_syntax", "error": "SyntaxError: invalid syntax (paper_broken_syntax.py, "
                                                              "line 1)", "frames": ["app/modules/paper_broken_syntax.py:1"]},
        {"module": "app.modules.paper_walk_forward_backtester", "error": err,
         "frames": ["app/modules/paper_walk_forward_backtester.py:1"]},
        {"module": "app.modules.paper_wire_engine_entry_module", "error": err,
         "frames": ["app/modules/paper_wire_engine_entry_module.py:1", "app/modules/paper_backtesting_entry_point.py:1",
                    "app/modules/paper_walk_forward_backtester.py:1"]},
    ]


@pytest.fixture
def job(monkeypatch):
    files = {p: (c, "f00ea97f:T9" if "backtester" in p else "T4") for p, c in MODULES.items()}
    files["tests/test_paper_fine.py"] = ("def test_x():\n    pass\n", "T4")
    monkeypatch.setattr(engine_source, "code_rows", AsyncMock(return_value=[{"node_key": "T4"}]))
    monkeypatch.setattr(engine_source, "job_files", lambda _rows: files)
    check = AsyncMock(return_value=et.EngineTestResult("fail", "importing its modules failed", [], imports=_failures()))
    monkeypatch.setattr(et, "check", check)
    insert = AsyncMock(side_effect=["DELIVERL1", "DELIVERL2"])
    monkeypatch.setattr(lt, "_insert_fix_step", insert)
    lt.clear_still_open("j")
    return check, insert


@pytest.mark.asyncio
async def test_a_module_that_does_not_import_gets_one_fix_step_for_the_file_the_failure_is_in(monkeypatch, job):
    check, insert = job
    monkeypatch.setattr(lt, "async_session", lambda: _Session([]))
    assert await lt.import_fix_steps("j", "DELIVER") == ["DELIVERL1", "DELIVERL2"]
    output = check.await_args.args[0]
    assert output.count("```python\n# app/modules/") == 6 and "tests/test_paper_fine.py" not in output
    titles = [c.args[3] for c in insert.await_args_list]
    assert titles == ["Fix paper_broken_syntax (it does not import with the job's modules)",
                      "Fix paper_walk_forward_backtester (it does not import with the job's modules)"]
    prompt = insert.await_args_list[1].args[4]
    assert prompt.startswith("Python module `paper_walk_forward_backtester.py` -- the earlier job's module: edit it in "
                             "place")
    assert prompt.count("cannot import name 'MAX_PROPOSALS_PER_BATCH'") == 3          # the three modules it breaks
    assert "`app.modules.paper_wire_engine_entry_module`" in prompt
    assert lt.still_open_note("j") == ""


@pytest.mark.asyncio
async def test_a_module_still_not_importing_after_its_fix_is_named_for_the_delivery_question(monkeypatch, job):
    _check, insert = job
    monkeypatch.setattr(lt, "async_session", lambda: _Session(
        [("Fix paper_walk_forward_backtester (it does not import with the job's modules)",)]))
    assert await lt.import_fix_steps("j", "DELIVER") == ["DELIVERL1"]
    assert [c.args[3] for c in insert.await_args_list] == [
        "Fix paper_broken_syntax (it does not import with the job's modules)"]
    note = lt.still_open_note("j")
    assert note.startswith("Before you answer -- the engine's checks before delivery left these open")
    assert ("- `app/modules/paper_walk_forward_backtester.py` does not import with the job's modules: "
            "`app.modules.paper_backtesting_entry_point`: ImportError: cannot import name 'MAX_PROPOSALS_PER_BATCH'") \
        in note
    assert note.rstrip().endswith("its fix step \"Fix paper_walk_forward_backtester (it does not import with the job's "
                                  "modules)\" ran once already")


@pytest.mark.asyncio
async def test_a_sandbox_that_did_not_run_is_named_not_taken_for_a_pass(monkeypatch, job):
    check, insert = job
    check.return_value = et.EngineTestResult("skip", "engine-test sandbox unreachable", [])
    monkeypatch.setattr(lt, "async_session", lambda: _Session([]))
    assert await lt.import_fix_steps("j", "DELIVER") == []
    insert.assert_not_awaited()
    assert "- the job's modules were not imported together (skip): engine-test sandbox unreachable" \
        in lt.still_open_note("j")
    check.return_value = et.EngineTestResult("pass", "imports cleanly", [])
    lt.clear_still_open("j")
    assert await lt.import_fix_steps("j", "DELIVER") == [] and lt.still_open_note("j") == ""


def test_the_delivery_pause_runs_the_import_check_after_the_together_run():
    src = inspect.getsource(ea._pause_for_decision)
    assert src.index("stale_test_steps(job_id") < src.index("import_fix_steps(job_id") \
        < src.index("lint_fix_steps(job_id")
