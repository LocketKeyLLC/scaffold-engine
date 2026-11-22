"""§17.1992 — a step that edits a module in place keeps the tests that module's file already holds.

Live (5b4880c0 FEX1 "Fix paper_wire_engine_entry_module (the /execution route passes no db, operator or time)",
2026-11-22 00:09 UTC): the step wrote `tests/test_paper_wire_engine_entry_module.py` with its 3 new route tests and
none of the 5 T9 had written (`test_install_registers_daily_batch_job`, …), and was recorded done -- the keep-list
(§17.1864 / §17.1891) held only the tests of the step's OWN earlier attempts. Now the module's test file as it stands
(the job's last writer, or the engine tree's reviewed copy after a delivery) is the baseline: a dropped test is put
back as it stands, the gate refuses one still missing by name, the prompt names them -- unless the step's own text
retires a named test."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.modules import engine_source
from app.modules.engine_contracts import (
    _dropped_names,
    dropped_module_tests,
    splice_kept,
    undefined_names,
    written_tests,
)
from app.sandbox.engine_tests import files_of

MOD = "paper_wire_engine_entry_module"
_REAL_ENGINE_TEST_FILE = engine_source._engine_test_file
PATH = f"tests/test_{MOD}.py"

MODULE = f'''# app/modules/{MOD}.py
ROUTER_PREFIX = "/labs/paper"
DAILY_BATCH_JOB_ID = "paper_daily_batch"


def get_scheduler():
    return None


def install(app) -> None:
    return None
'''

# T9's test file, as the live one is shaped: a fixture, a fake class, a helper, five tests
T9_TESTS = f'''# {PATH}
"""Tests for app.modules.{MOD}."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI

from app.modules.{MOD} import (
    DAILY_BATCH_JOB_ID,
    ROUTER_PREFIX,
    install,
)


@pytest.fixture
def fastapi_app() -> FastAPI:
    return FastAPI()


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func, **kwargs) -> None:
        self.jobs.append({{"func": func, **kwargs}})


def _route_paths(app: FastAPI) -> list[str]:
    return list(app.openapi()["paths"].keys())


@pytest.mark.asyncio
async def test_install_registers_daily_batch_job(fastapi_app: FastAPI):
    fake = FakeScheduler()
    with patch("app.modules.{MOD}.get_scheduler", return_value=fake):
        install(fastapi_app)
    assert fake.jobs[0]["id"] == DAILY_BATCH_JOB_ID


@pytest.mark.asyncio
async def test_install_registers_routes(fastapi_app: FastAPI):
    install(fastapi_app)
    assert f"{{ROUTER_PREFIX}}/execution" in _route_paths(fastapi_app)


@pytest.mark.asyncio
async def test_install_no_scheduler_does_not_schedule(fastapi_app: FastAPI):
    with patch("app.modules.{MOD}.get_scheduler", return_value=None):
        install(fastapi_app)


@pytest.mark.asyncio
async def test_install_starts_stream_consumer_task(fastapi_app: FastAPI):
    install(fastapi_app)


@pytest.mark.asyncio
async def test_install_does_not_read_secrets(fastapi_app: FastAPI):
    install(fastapi_app)
'''
T9_NAMES = ["test_install_registers_daily_batch_job", "test_install_registers_routes",
            "test_install_no_scheduler_does_not_schedule", "test_install_starts_stream_consumer_task",
            "test_install_does_not_read_secrets"]

# FEX1's answer, as live: the module, and the test file with only its three new route tests (imported `as mod`)
FEX1_TESTS = f'''# {PATH}
"""Tests for the paper lab's engine wiring."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.modules import {MOD} as mod


class FakePrincipal:
    def __init__(self, identity: str) -> None:
        self.identity = identity


@pytest.mark.asyncio
async def test_execution_route_passes_identity_timestamp_and_session():
    assert mod.ROUTER_PREFIX


@pytest.mark.asyncio
async def test_execution_route_opens_its_own_session():
    assert FakePrincipal("operator-7").identity == "operator-7"


def test_execution_route_uses_the_principal_from_the_engine_dependency():
    with patch.object(mod, "get_scheduler", return_value=None):
        assert mod.get_scheduler() is None
'''
FIX_TITLE = f"Fix {MOD} (the /execution route passes no db, operator or time)"
FIX_TEXT = (f"Fix {MOD}: the POST /execution route calls run_execution_entry_point() with none of its required "
            "keyword arguments. Keep every other route as it is. Tests: the route with a fake principal calls "
            "run_execution_entry_point with that identity, a UTC timestamp and a session.")


def _out(*blocks: str) -> str:
    return "\n\n".join(f"```python\n{b}```" for b in blocks)


def _rows(fix_text: str = FIX_TEXT, title: str = FIX_TITLE) -> tuple[list[dict], dict]:
    t9 = {"node_key": "T9", "title": "Wire engine entry module", "status": "done",
          "output_text": _out(MODULE, T9_TESTS), "completed_at": datetime(2026, 11, 4, 23, 34, tzinfo=timezone.utc)}
    fex1 = {"node_key": "FEX1", "title": title, "prompt_template": fix_text, "status": "running", "output_text": None}
    return [t9, fex1], fex1


@pytest.fixture(autouse=True)
def _no_engine_tree_copy(monkeypatch):
    """The engine tree's copy is read only where a test asks for it (the dev image carries the delivered lab's)."""
    monkeypatch.setattr(engine_source, "_engine_test_file", lambda module: None)


# ── the live shape ───────────────────────────────────────────────────────────────────────────────────────────────

def test_a_fix_step_inherits_the_tests_the_module_file_already_holds():
    rows, fex1 = _rows()
    mt = engine_source.module_tests(rows, fex1)
    assert (mt["module"], mt["path"], mt["key"], mt["tests"], mt["retired"]) == (MOD, PATH, "T9", T9_NAMES, []), mt


def test_an_answer_that_drops_them_is_refused_by_name():
    rows, fex1 = _rows()
    mt = engine_source.module_tests(rows, fex1)
    found = dropped_module_tests(_out(MODULE, FEX1_TESTS), mt)
    assert len(found) == 1, found
    assert found[0].startswith(f"`{PATH}` dropped 5 of the 5 tests it already held for `{MOD}`"), found
    assert "step T9 wrote them" in found[0], found
    assert _dropped_names(found[0]) == T9_NAMES          # §17.1877's reader sees the names (the redraw asks for them)
    # one the step's own attempts wrote is the §17.1864 finding's, not reported twice
    assert _dropped_names(dropped_module_tests(_out(MODULE, FEX1_TESTS), mt, {T9_NAMES[0]})[0]) == T9_NAMES[1:]


def test_a_dropped_test_is_put_back_as_it_stands_with_what_it_stands_on():
    rows, fex1 = _rows()
    mt = engine_source.module_tests(rows, fex1)
    passing = engine_source.with_module_tests({}, {}, mt)
    assert mt["inherited"] == T9_NAMES
    answer = _out(MODULE, FEX1_TESTS)
    spliced, names = splice_kept(answer, engine_source.inherited_kept(answer, passing, mt))
    assert sorted(names) == sorted(f"{PATH}::{n}" for n in T9_NAMES), names
    code = files_of({"output_text": spliced})[PATH]
    compile(code, PATH, "exec")
    assert written_tests(spliced)[PATH] == [
        "test_execution_route_passes_identity_timestamp_and_session", "test_execution_route_opens_its_own_session",
        "test_execution_route_uses_the_principal_from_the_engine_dependency", *T9_NAMES]
    # its fixture, fake class and helper came with it, and the names it imports (the answer imported `as mod`)
    for need in ("def fastapi_app(", "class FakeScheduler", "def _route_paths(", "import FastAPI",
                 "DAILY_BATCH_JOB_ID, ROUTER_PREFIX, install"):
        assert need in code, need
    assert undefined_names({PATH: code}) == [], undefined_names({PATH: code})
    assert code.index("from __future__") < code.index("import FastAPI")        # never an import above __future__
    assert dropped_module_tests(spliced, mt) == []


def test_an_inherited_test_the_answer_rewrites_is_its_own():
    """The fix may make an old test wrong: the answer's version stands, only a dropped one is put back."""
    rows, fex1 = _rows()
    mt = engine_source.module_tests(rows, fex1)
    passing = engine_source.with_module_tests({}, {}, mt)
    mine = FEX1_TESTS + '''

def test_install_registers_routes():
    assert mod.ROUTER_PREFIX == "/labs/paper"
'''
    from app.modules.execution_agent import _splice_kept_tests       # the run's own splice
    spliced = _splice_kept_tests(_out(MODULE, mine), {"prior_passing": passing, "module_tests": mt}, "FEX1")
    code = files_of({"output_text": spliced})[PATH]
    assert 'assert mod.ROUTER_PREFIX == "/labs/paper"' in code
    assert "_route_paths(fastapi_app)" not in code                     # T9's version did not come back over it
    assert set(T9_NAMES) <= set(written_tests(spliced)[PATH])           # the four it dropped did


def test_the_prompt_names_the_tests_to_keep_apart_from_the_passed_ones():
    rows, fex1 = _rows()
    mt = engine_source.module_tests(rows, fex1)
    passing = engine_source.with_module_tests({}, {}, mt)
    block = engine_source.module_tests_block(mt)
    assert f"## The tests `{PATH}` already holds (step T9) -- keep every one" in block
    assert all(f"`{n}`" in block for n in T9_NAMES)
    out = engine_source.block_for({**fex1, "prior_passing": passing, "module_tests": mt}, [])
    assert "Tests that PASSED in the last run" not in out          # they never ran in this step


def test_the_steps_own_record_governs_once_an_attempt_carried_them():
    """Once an attempt wrote them, the step's own record (§17.1864 names, §17.1891 sources) is what holds them."""
    rows, fex1 = _rows()
    mt = engine_source.module_tests(rows, fex1)
    assert engine_source.with_module_tests({}, {PATH: list(T9_NAMES)}, mt) == {}
    assert mt["inherited"] == []


# ── correct misses ───────────────────────────────────────────────────────────────────────────────────────────────

def test_a_step_writing_a_new_modules_first_tests_inherits_nothing():
    rows, _ = _rows()
    new = {"node_key": "T12", "title": "Paper risk limits module", "status": "running",
           "prompt_template": "Python module `paper_risk_limits.py` with its tests."}
    assert engine_source.module_tests(rows + [new], new) == {}
    # and a fix of a module whose file no step wrote and the engine has none of
    fix = {"node_key": "F2", "title": "Fix paper_risk_limits (caps are per day)", "status": "running"}
    other = {"node_key": "T12", "status": "done", "output_text": _out("# app/modules/paper_risk_limits.py\nX = 1\n")}
    assert engine_source.module_tests(rows + [other, fix], fix) == {}


def test_a_step_told_to_replace_a_named_test_is_not_held_to_it():
    text = FIX_TEXT + " Replace test_install_registers_routes with one test per route prefix."
    rows, fex1 = _rows(text)
    mt = engine_source.module_tests(rows, fex1)
    assert mt["retired"] == ["test_install_registers_routes"]
    assert "test_install_registers_routes" not in mt["tests"]
    found = dropped_module_tests(_out(MODULE, FEX1_TESTS), mt)
    assert "test_install_registers_routes" not in _dropped_names(found[0])
    assert "Your task retires `test_install_registers_routes`" in engine_source.module_tests_block(mt)
    # a name merely mentioned is not retired
    rows, fex1 = _rows(FIX_TEXT + " test_install_registers_routes must still pass.")
    assert engine_source.module_tests(rows, fex1)["retired"] == []


def test_an_answer_that_leaves_the_test_file_alone_keeps_it():
    rows, fex1 = _rows()
    mt = engine_source.module_tests(rows, fex1)
    assert dropped_module_tests(_out(MODULE), mt) == []


def test_the_original_writer_is_not_its_own_baseline():
    rows, _ = _rows()
    assert engine_source.module_tests(rows, rows[0]) == {}


# ── §17.1988b the engine tree's reviewed copy after a delivery ─────────────────────────────────────────────────────

def test_after_a_delivery_the_engine_trees_copy_is_the_baseline(tmp_path, monkeypatch):
    lab = tmp_path / "tests" / "labs" / "paper"
    lab.mkdir(parents=True)
    (lab / f"test_{MOD}.py").write_text(T9_TESTS.replace("test_install_does_not_read_secrets",
                                                        "test_install_reads_no_secret_at_import"))
    monkeypatch.setattr(engine_source, "ENGINE_PKG", tmp_path / "app")
    monkeypatch.setattr(engine_source, "_engine_test_file", _REAL_ENGINE_TEST_FILE)
    rows, fex1 = _rows()
    deliver = {"node_key": "D1", "title": "Is this job's code merged into the engine and running?", "status": "done",
               "completed_at": datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)}
    mt = engine_source.module_tests([*rows[:1], deliver, rows[1]], fex1)
    assert mt["key"] == "T9, as in the engine's tree", mt
    assert "test_install_reads_no_secret_at_import" in mt["tests"]
    # not delivered since T9 wrote it: the job's copy
    assert engine_source.module_tests(rows, fex1)["key"] == "T9"


# ── the wiring: the step's run and the findings read the job's rows ───────────────────────────────────────────────

@pytest.mark.integration
async def test_the_run_and_the_findings_read_the_module_tests_from_the_job():
    import json
    import uuid

    from app.database import async_session
    from app.modules import execution_agent, node_editor
    text = node_editor.text
    jid = str(uuid.uuid4())
    brief = {"title": "lab", "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                              "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(brief)})
        for i, (k, title, st, prompt, out) in enumerate([
                ("T9", "Wire engine entry module", "done", f"Python module `{MOD}.py`.", _out(MODULE, T9_TESTS)),
                ("FEX1", FIX_TITLE, "running", FIX_TEXT, None)]):
            await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, output_text, tool, "
                "execution_order) VALUES (:j, :k, :t, :s, :p, :o, 'CodeGen', :n)"),
                {"j": jid, "k": k, "t": title, "s": st, "p": prompt, "o": out, "n": i})
        await db.commit()
    try:
        async with async_session() as db:
            found = await engine_source.engine_findings(db, jid, "FEX1", _out(MODULE, FEX1_TESTS))
        assert any(f.startswith(f"`{PATH}` dropped 5 of the 5 tests it already held") for f in found), found
        snap = {"node_key": "FEX1", "title": FIX_TITLE, "prompt_template": FIX_TEXT, "prior_tests": {},
                "prior_passing": {}}
        async with async_session() as db:
            await execution_agent._inherit_module_tests(db, jid, snap, {"tool_config": None}, "CodeGen")
        assert snap["module_tests"]["tests"] == T9_NAMES
        spliced = execution_agent._splice_kept_tests(_out(MODULE, FEX1_TESTS), snap, "FEX1")
        assert set(T9_NAMES) <= set(written_tests(spliced)[PATH])
        async with async_session() as db:
            assert not any("already held" in f
                           for f in await engine_source.engine_findings(db, jid, "FEX1", spliced))
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
