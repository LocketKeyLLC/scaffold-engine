"""§17.1998 — a fix step of a lab's follow-up job keeps the lab's tests, and the delivery never deletes them.

Live (69fe61c4 T8 "Add dashboard route", 2026-11-22 16:49–16:58 UTC, `tool_config.fixes_module` =
`paper_wire_engine_entry_module`): the job builds on no other job and none of its steps wrote the module's test file,
so §17.1992's `module_tests` fell back to the engine tree's copy (`tests/labs/paper/…`) -- which the runtime image did
not carry (no tests at all). No prompt of T8's 50 attempts named the 8 tests, the keep-list was empty, and the file came
back with 15 tests and none of the 8 on main; `deliver_job.py` wrote it over the lab's. Now the runtime image carries
the labs' tests, and the delivery puts back each test the file holds that the job's copy dropped, loudly."""
from __future__ import annotations

import ast
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.modules import engine_source

REPO = Path(__file__).resolve().parent.parent
MOD = "paper_wire_engine_entry_module"
LAB_TEST = REPO / "tests" / "labs" / "paper" / f"test_{MOD}.py"
#: the lab's file as it stood when T8 ran (main 328ecb17): the delivery tests below are about THAT file -- the tree's
#: copy grows as the lab does (the dashboard's delivery, #1191, made it 46 tests)
LAB_BEFORE = REPO / "tests" / "fixtures" / "lab_paper_entry_tests_before_the_dashboard_2026_11_22.py.txt"
MAIN_TESTS = [
    "test_install_registers_daily_batch_job", "test_install_registers_routes",
    "test_install_no_scheduler_does_not_schedule", "test_install_starts_stream_consumer_task",
    "test_install_does_not_read_secrets", "test_execution_route_passes_identity_timestamp_and_session",
    "test_execution_route_opens_its_own_session", "test_execution_route_uses_the_principal_from_the_engine_dependency",
]


# ── the engine end: `module_tests` reads the lab's file from the engine tree (the image's copy: the host-only
#    test_the_runtime_image_carries_the_labs_tests_17_1998.py) ───────────────────────────────────────────────

def test_a_follow_up_jobs_fix_step_inherits_the_labs_tests_from_the_engine_tree():
    """The live shape: the job's own rows only (no `builds_on`), the fix step names its module by `fixes_module`."""
    assert LAB_TEST.is_file()
    rows = [
        {"node_key": "T5", "tool": "CodeGen", "title": "Write metrics computation module", "status": "done",
         "output_text": "```python\n# app/modules/paper_dashboard_metrics.py\nX = 1\n```",
         "completed_at": datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)},
        {"node_key": "T8", "tool": "CodeGen", "title": "Add dashboard route", "status": "running", "output_text": None,
         "tool_config": {"fixes_module": MOD}},
    ]
    mt = engine_source.module_tests(rows, rows[1])
    assert mt["path"] == f"tests/test_{MOD}.py" and mt["key"] == "as in the engine's tree", mt
    assert set(MAIN_TESTS) <= set(mt["tests"]), mt["tests"]
    assert "keep every one" in engine_source.module_tests_block(mt)


def test_a_module_with_no_lab_test_file_inherits_nothing(tmp_path, monkeypatch):
    """The correct miss: no file in the tree (nor in the job) -- nothing to keep."""
    monkeypatch.setattr(engine_source, "ENGINE_PKG", tmp_path / "app")
    node = {"node_key": "T8", "tool": "CodeGen", "title": "Add dashboard route", "tool_config": {"fixes_module": MOD}}
    assert engine_source.module_tests([node], node) == {}


# ── the delivery end ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def dj(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("deliver_job_1998", REPO / "scripts" / "deliver_job.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    (tmp_path / "app" / "modules").mkdir(parents=True)
    (tmp_path / "app" / "labs.py").write_text("LABS: tuple[str, ...] = ()\n")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


# the job's copy, shaped as T8's: its own imports, a fixture of its own, new route tests, none of the 8
JOB_FILE = f'''"""Tests for the dashboard route."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.{MOD} import ROUTER_PREFIX, install


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    with patch("app.modules.{MOD}.get_scheduler", return_value=None):
        install(app)
    return TestClient(app)


def test_dashboard_route_is_registered(client):
    assert client.app is not None
    assert ROUTER_PREFIX


def test_dashboard_route_renders(client):
    assert client is not None
'''


def test_the_delivery_puts_back_every_test_the_job_dropped(dj):
    current = LAB_BEFORE.read_text()
    text, back = dj.keep_tests(current, JOB_FILE)
    assert back == [t for t in dj.test_names(current) if t in MAIN_TESTS]
    assert set(MAIN_TESTS) <= set(dj.test_names(text))
    assert {"test_dashboard_route_is_registered", "test_dashboard_route_renders"} <= set(dj.test_names(text))
    tree = ast.parse(text)                                     # and every name the kept tests read is bound
    bound = set().union(*(dj._bound(n) for n in tree.body))
    import builtins
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in MAIN_TESTS:
            local = {a.arg for f in ast.walk(n) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                     for a in (*f.args.posonlyargs, *f.args.args, *f.args.kwonlyargs)} | {
                x.id for x in ast.walk(n) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)} | {
                x.asname or x.name for f in ast.walk(n) if isinstance(f, (ast.Import, ast.ImportFrom))
                for x in f.names} | {
                i.optional_vars.id for w in ast.walk(n) if isinstance(w, (ast.With, ast.AsyncWith))
                for i in w.items if isinstance(i.optional_vars, ast.Name)} | {
                f.name for f in ast.walk(n) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            free = {x.id for x in ast.walk(n) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
            assert free <= bound | local | set(dir(builtins)), (n.name, free - bound - local - set(dir(builtins)))


def test_a_job_copy_that_keeps_every_test_is_written_as_it_is(dj):
    """The correct miss: nothing dropped, nothing added."""
    current = LAB_BEFORE.read_text()
    new = current + "\n\ndef test_a_new_one():\n    assert True\n"
    assert dj.keep_tests(current, new) == (new, [])


def test_the_live_delivery_keeps_the_tests_and_says_so(dj, tmp_path, capsys):
    dest = tmp_path / "tests" / "labs" / "paper" / f"test_{MOD}.py"
    dest.parent.mkdir(parents=True)
    dest.write_text(LAB_BEFORE.read_text())
    out = {"files": [{"path": f"tests/test_{MOD}.py", "step": "T8", "lines": JOB_FILE.count("\n"),
                      "content": JOB_FILE},
                     {"path": f"app/modules/{MOD}.py", "step": "T8", "lines": 1, "content": "X = 1\n"}],
           "tables": [], "secrets": [], "wiring": [MOD], "delivered_at": None}
    assert dj.deliver(out, MOD) == 1                            # for the review: tests were put back
    assert set(MAIN_TESTS) <= set(dj.test_names(dest.read_text()))
    err = capsys.readouterr().err
    assert f"KEPT 8 test(s) in tests/labs/paper/test_{MOD}.py that the job's copy dropped" in err
