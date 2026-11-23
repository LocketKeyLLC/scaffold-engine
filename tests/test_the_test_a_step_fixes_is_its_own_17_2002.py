"""§17.2002 — the test a step is there to fix is the step's to change: kept by name, never spliced back over its answer.

Live (dashboard job 69fe61c4, T12L1, 2026-11-22 23:45 – 2026-11-23 00:01 UTC, after #1189): T12L1 was added to fix
`test_load_series_reads_committed_pnl_rows_oldest_first`, which failed only when run with the job's other tests. In
the step's own sandbox it passed (alone), so §17.1891 kept it as passing and spliced that body back over every later
draft (`kept_passing_tests_spliced node=T12L1 n=45`). Each draft rewrote it (llm_traces 13677: two renders compared
per series); the verifier was shown the old single-render body and refused three times -- "the failing test … was
never actually changed" -- and the job blocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.modules import engine_contracts, engine_source
from app.modules import execution_agent as xa
from app.modules import lab_together as lt

PATH = "tests/test_paper_wire_engine_entry_module.py"
NAME = "test_load_series_reads_committed_pnl_rows_oldest_first"
#: T12L1's own text, as the engine added it (dag_nodes.prompt_template)
PROMPT = "Python test module `tests/test_paper_wire_engine_entry_module.py` -- step REACH's tests: edit it in place (the whole file, every test kept). Run with the job's other tests before delivery (1 failed, 193 passed in 3.30s), these fail:\n- `test_load_series_reads_committed_pnl_rows_oldest_first`\nEach passes alone or did when its step ran: look for what one test leaves behind for the next (rows, a patched name, an open transaction, a module-level cache) or assumes about order. They run before the step passes, with no network or secrets; `async_session` is a scratch database with this job's tables (each test starts with them empty and inserts the rows it needs; never DELETE from them yourself); HTTP clients and model calls are mocked. Change the tests only: the modules they test are finished."
#: the body the step's sandbox recorded as passing (alone) -- what the verifier was shown (llm_traces 13678)
OLD = 'def test_load_series_reads_committed_pnl_rows_oldest_first(spy_benchmark):\n    """The chart\'s series is built from the lab\'s committed rows, oldest first.\n\n    The scratch database is empty, so the series is empty and trivially ordered;\n    what this pins is that the entry module builds it per request, from the\n    read-only session, rather than from anything an earlier request left behind.\n    """\n    app = _lab_app()\n    with _no_template() as seen:\n        resp = TestClient(app).get(DASHBOARD_PATH)\n    assert resp.status_code == 200\n    stamps = _series_timestamps(seen)\n    assert stamps == sorted(stamps)'
#: the draft's rewrite (llm_traces 13677)
NEW = 'def test_load_series_reads_committed_pnl_rows_oldest_first(spy_benchmark):\n    """The chart\'s series is built from the lab\'s committed rows, oldest first.\n\n    Two identical requests are rendered and each render\'s series is compared as\n    ISO text, per series: no naive stamp is ever lined up against an aware one,\n    and one series\' rows are never sorted against another\'s. What this pins is\n    that the entry module rebuilds the series per request -- from the read-only\n    session, in order -- rather than serving whatever an earlier request left\n    behind. The scratch database is empty, so a series here is the empty list\n    and the ordering check is trivially true; the point is that it cannot break\n    on the kind of stamp a shared row happens to carry.\n    """\n    app = _lab_app()\n    with _render_captures() as renders:\n        client = TestClient(app)\n        first = client.get(DASHBOARD_PATH)\n        second = client.get(DASHBOARD_PATH)\n    assert first.status_code == 200\n    assert second.status_code == 200\n    assert len(renders) == 2, "the dashboard did not render once per request"\n    rebuilt = _ordered_stamps(renders[0])\n    again = _ordered_stamps(renders[1])\n    assert rebuilt == again, "two identical requests produced different series"\n    for stamps in rebuilt:\n        assert stamps == sorted(stamps), f"a series is not oldest first: {stamps}"'
OTHER = "def test_install_registers_routes():\n    assert True\n"
#: the live file defines the fixture both bodies take (§17.1920 splices no test whose fixture is defined nowhere)
#: and the helpers both read (§17.1920 splices no test that reads a name the file binds nowhere)
FIXTURE = ("import pytest\nfrom fastapi.testclient import TestClient\n\nDASHBOARD_PATH = '/labs/paper/dashboard'\n\n\n"
           "@pytest.fixture\ndef spy_benchmark():\n    return None\n\n\n"
           + "".join(f"def {h}(*a):\n    return None\n\n\n" for h in
                     ("_lab_app", "_no_template", "_series_timestamps", "_render_captures", "_ordered_stamps")))


def _answer(body: str) -> str:
    return f"```python\n# {PATH}\n{FIXTURE}\n\n{body}\n\n\n{OTHER}```\n"


def test_the_step_added_before_this_names_its_test_in_its_own_text():
    assert engine_source.tests_to_fix({"prompt_template": PROMPT, "tool_config": None}) == {NAME}


def test_the_engine_marks_and_the_stale_reset_marks_are_read():
    row = {"prompt_template": "Write it.", "tool_config": {"fails_together": ["TestRoutes::test_a", "test_b[x]"],
                                                           lt.RERAN_KEY: ["test_c"]}}
    assert engine_source.tests_to_fix(row) == {"TestRoutes", "test_b", "test_c"}
    assert lt.RERAN_KEY in engine_source._TESTS_TO_FIX_KEYS
    assert engine_source.tests_to_fix({"prompt_template": "Fix the dashboard.", "tool_config": {}}) == set()


def test_the_answers_rewrite_of_the_test_to_fix_is_what_runs():
    kept = {PATH: {NAME: OLD, "test_install_registers_routes": OTHER}}
    snap = {"prior_passing": kept, "tests_to_fix": [NAME]}
    out = xa._splice_kept_tests(_answer(NEW), snap, "T12L1")
    assert "with _render_captures() as renders:" in out and "with _no_template() as seen:" not in out
    # before: the passing-alone copy replaced the rewrite
    out = xa._splice_kept_tests(_answer(NEW), {"prior_passing": kept}, "T12L1")
    assert "with _no_template() as seen:" in out and "with _render_captures() as renders:" not in out


def test_the_test_to_fix_is_still_held_by_name():
    mt = {"module": "paper_wire_engine_entry_module", "path": PATH, "key": "REACH", "tests": [NAME,
          "test_install_registers_routes"], "retired": [], "tests_only": True, "to_fix": [NAME]}
    found = engine_contracts.dropped_module_tests(f"```python\n# {PATH}\n{OTHER}```\n", mt)
    assert found and f"`{NAME}`" in found[0]
    block = engine_source.module_tests_block(mt)
    assert f"This step is here to fix `{NAME}`: rewrite it (same name) -- your version is the one that runs." in block


def test_without_tests_to_fix_leaves_the_rest_of_the_keep_list():
    kept = {PATH: {NAME: OLD, "test_install_registers_routes": OTHER}, "tests/test_x.py": {NAME: "x"}}
    assert engine_source.without_tests_to_fix(kept, {NAME}) == {
        PATH: {"test_install_registers_routes": OTHER}, "tests/test_x.py": {}}
    assert engine_source.without_tests_to_fix(kept, set()) is kept


@pytest.mark.asyncio
async def test_the_snapshot_carries_the_tests_to_fix(monkeypatch):
    monkeypatch.setattr(engine_source, "code_rows", AsyncMock(return_value=[]))
    snap = {"node_key": "T12L1", "title": "Fix test_paper_wire_engine_entry_module (its tests fail with the job's)",
            "prompt_template": PROMPT}
    await xa._inherit_module_tests(object(), "j", snap, {"tool_config": None}, "CodeGen")
    assert snap["tests_to_fix"] == [NAME]


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a, **_k):
        rows = self.rows

        class _R:
            def all(self):
                return rows
        return _R()


@pytest.mark.asyncio
async def test_the_engines_test_fix_step_records_the_tests_it_fixes(monkeypatch):
    insert = AsyncMock(return_value="T12L1")
    monkeypatch.setattr(lt, "_insert_fix_step", insert)
    monkeypatch.setattr(lt, "async_session", lambda: _Session([("REACH", "Use T9, T10 from the lab's entry")]))
    await lt.failing_test_fix_steps("j", "T12", [("REACH", PATH, [NAME])], "1 failed")
    assert insert.await_args.kwargs["tool_config"] == {"fixes_tests_of": "paper_wire_engine_entry_module",
                                                       "fails_together": [NAME]}
