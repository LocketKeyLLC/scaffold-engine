"""§17.1891 — a test that passed in an earlier attempt is kept as it passed: the sandbox names every passing test
(`-rA`), the failed attempt's log carries their source, the next attempt is shown their names (and some of their
bodies, for the helpers they use), and after its draw the engine splices them back by name -- so an attempt rewrites
only what failed and what is new, and a passing test cannot regress.

Live (follow-up 5b4880c0 T5, 2026-11-02 21:01–21:23 UTC): a1 failed 3 of 31, a2 1 of 32, a3 2, a4 10 -- every
attempt redrew every body from scratch (the keep-list held names only, §17.1864), and tests that had passed came back
failing. "Thrash means a missing checkpoint" (§17.1050)."""
from __future__ import annotations

import inspect

import pytest

from app.modules import engine_source as es
from app.modules import execution_agent as ea
from app.modules.engine_contracts import kept_test_sources, splice_kept
from app.sandbox import engine_tests as et

STDOUT = """..F.                                                                     [100%]
=================================== FAILURES ===================================
____________________________ TestK.test_b ______________________________________
E   assert False
=========================== short test summary info ============================
PASSED tests/test_paper_twelve_modules.py::TestK::test_a
PASSED tests/test_paper_twelve_modules.py::test_c
PASSED tests/test_paper_twelve_modules.py::test_c
FAILED tests/test_paper_twelve_modules.py::TestK::test_b - assert False
FAILED tests/test_paper_twelve_modules.py::test_d - assert 1 == 2
2 failed, 2 passed in 0.94s
"""

OUTPUT = '''```python
# tests/test_paper_twelve_modules.py
import pytest
from app.modules.paper_fill_tracker import is_trade_update


def _envelope():
    return {"stream": "trade_updates"}


class TestK:
    def test_a(self):
        assert is_trade_update(_envelope())

    def test_b(self):
        assert False


@pytest.mark.asyncio
async def test_c():
    assert is_trade_update(_envelope()) is True


def test_d():
    assert 1 == 2
```
'''


def test_the_sandbox_names_every_passing_test_and_the_report_does_not_repeat_them():
    assert et.passed_tests(STDOUT) == ["tests/test_paper_twelve_modules.py::TestK::test_a",
                                       "tests/test_paper_twelve_modules.py::test_c"]
    assert et.passed_tests("") == []
    src = inspect.getsource(et.check)
    assert '"-q", "-rA", "--tb=short"' in src                               # pytest names the passing ones
    report = et._report(STDOUT, budget=600)
    assert "PASSED " not in report and "FAILED tests/test_paper_twelve_modules.py::test_d" in report
    res = et.EngineTestResult("fail", "x", ["tests/t.py"])
    assert res.passed == []                                                 # the field, default empty


def test_a_passing_top_level_test_is_kept_with_its_decorators_and_a_class_only_whole():
    kept = kept_test_sources(OUTPUT, et.passed_tests(STDOUT))
    assert kept == {"tests/test_paper_twelve_modules.py": {
        "test_c": "@pytest.mark.asyncio\nasync def test_c():\n    assert is_trade_update(_envelope()) is True",
        "_envelope": 'def _envelope():\n    return {"stream": "trade_updates"}'}}     # §17.1916 what it calls
    # every method of the class passed: the class is kept whole
    both = kept_test_sources(OUTPUT, ["tests/test_paper_twelve_modules.py::TestK::test_a",
                                      "tests/test_paper_twelve_modules.py::TestK::test_b"])
    assert set(both["tests/test_paper_twelve_modules.py"]) == {"TestK", "_envelope"}          # §17.1916 its helper
    assert both["tests/test_paper_twelve_modules.py"]["TestK"].startswith("class TestK:\n    def test_a(self):")
    assert kept_test_sources(OUTPUT, ["tests/other.py::test_c"]) == {}    # another file: nothing of this one
    assert kept_test_sources("no code here", ["tests/test_paper_twelve_modules.py::test_c"]) == {}


def test_the_kept_tests_are_put_back_as_they_passed_and_a_missing_one_is_appended():
    kept = {"tests/test_paper_twelve_modules.py": {
        "test_c": "@pytest.mark.asyncio\nasync def test_c():\n    assert is_trade_update(_envelope()) is True",
        "test_e": "def test_e():\n    assert _envelope()[\"stream\"] == \"trade_updates\""}}
    redrawn = OUTPUT.replace("async def test_c():\n    assert is_trade_update(_envelope()) is True",
                             "async def test_c():\n    assert is_trade_update({}) is True")     # regressed by the draw
    out, names = splice_kept(redrawn, kept)
    assert names == ["tests/test_paper_twelve_modules.py::test_c", "tests/test_paper_twelve_modules.py::test_e"]
    assert "assert is_trade_update(_envelope()) is True" in out and "is_trade_update({})" not in out
    assert out.count("def test_c(") == 1 and "def test_e():" in out and "def test_d():" in out
    # nothing kept, or the file not in the output: untouched
    assert splice_kept(OUTPUT, {}) == (OUTPUT, [])
    assert splice_kept(OUTPUT, {"tests/other.py": {"test_z": "def test_z():\n    pass"}}) == (OUTPUT, [])
    # already as it passed: untouched, nothing named
    assert splice_kept(OUTPUT, {"tests/test_paper_twelve_modules.py": {"test_c": kept["tests/test_paper_twelve_modules.py"]["test_c"]}}) == (OUTPUT, [])


def test_the_next_attempt_is_shown_the_kept_names_first_and_some_bodies_last():
    node = {"node_key": "T5", "title": "t", "description": "", "prompt_template": "Write tests.", "output_text": None,
            "prior_tests": {"tests/test_paper_twelve_modules.py": ["test_c", "test_d"]},
            "prior_passing": {"tests/test_paper_twelve_modules.py": {"test_c": "def test_c():\n    assert True"}}}
    block = es.block_for(node, set())
    head = block.index("### Tests that PASSED in the last run -- kept as written")
    assert block.index("### Tests your earlier attempts wrote") < head
    assert "    tests/test_paper_twelve_modules.py: test_c\n" in block
    assert "do not write them again; write the tests the sandbox named and any new ones" in block
    tail = block.index("### Some of the kept tests, as they passed")
    assert tail > head and "# tests/test_paper_twelve_modules.py -- test_c\ndef test_c():\n    assert True" in block
    assert block.rindex("### ") == tail                                     # the bodies come last: cut first
    # a long body is left out of the shown part, never out of the names
    big = {**node, "prior_passing": {"tests/test_paper_twelve_modules.py": {"test_big": "def test_big():\n" + "    x = 1\n" * 900}}}
    b2 = es.block_for(big, set())
    assert "    tests/test_paper_twelve_modules.py: test_big\n" in b2 and "### Some of the kept tests" not in b2


async def test_the_latest_verdict_of_each_name_wins_and_survives_a_reset():
    import json

    class _Res:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class _DB:
        def __init__(self, rows):
            self.rows, self.sql = rows, ""

        async def execute(self, q, params):
            self.sql = str(q)
            return _Res(self.rows)

    db = _DB([json.dumps({"tests/t.py": {"test_a": "def test_a():\n    assert 1", "test_b": "def test_b():\n    pass"}}),
              json.dumps({"tests/t.py": {"test_a": "def test_a():\n    assert 2"}}), "not json"])
    got = await ea.prior_passing_tests(db, "job", "node")
    assert got == {"tests/t.py": {"test_a": "def test_a():\n    assert 2", "test_b": "def test_b():\n    pass"}}
    # §17.1896 -- what passed survives a reset of either kind: the window is the step's own (replan, restore, a new text)
    assert "passed_tests" in db.sql and "e.op IN ('replan', 'restore')" in db.sql and "'reset'" not in db.sql


def test_the_failed_attempt_records_what_passed_and_the_next_draw_splices_before_any_gate():
    src = inspect.getsource(ea.execute_next_node)
    assert src.index("_passed_tests = _kept_after_check(output, _et_res)") > src.index("_et_res = await _et.check(")
    assert '"passed_tests": _passed_tests' in src
    assert 'node_snapshot["prior_passing"] = await prior_passing_tests(' in src
    # the splice wraps `_complete_required_tests` (every return of the draw goes through it -- §17.1892: it sat after
    # an early return and never ran live), which runs before the syntax gate and every other
    assert src.index("output = await _complete_required_tests(") < src.index("codegen_syntax_gate_error")
    crt = inspect.getsource(ea._complete_required_tests)
    assert crt.index("await _draw_required_tests(") < crt.index("return _splice_kept_tests(output, node_snapshot, node_key)")
    assert "splice_kept(output, node_snapshot[\"prior_passing\"])" in inspect.getsource(ea._splice_kept_tests)
    assert ea._kept_after_check("x", type("R", (), {"passed": []})()) == {}
    assert ea._splice_kept_tests("out", {}, "T5") == "out"


@pytest.mark.integration
def test_pytest_itself_names_passing_tests_this_way(tmp_path):
    import subprocess
    import sys
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fmt.py").write_text("class TestK:\n    def test_a(self):\n        assert True\n"
                                                    "def test_c():\n    assert True\ndef test_d():\n    assert False\n")
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rA", "--tb=short", "-p", "no:cacheprovider", "-c",
                          "/dev/null", "--rootdir", ".", "tests/test_fmt.py"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=120)
    got = et.passed_tests(run.stdout)
    assert got == ["tests/test_fmt.py::TestK::test_a", "tests/test_fmt.py::test_c"], run.stdout


def test_a_whole_file_redraw_is_told_what_is_kept_and_gets_it_back():
    """§17.1899 — live (5b4880c0 T5 a1, 2026-11-03 23:44 UTC): the whole-file redraw re-emitted all 93 kept tests
    (28k tokens, two minutes), and nothing put the kept versions back after a redraw."""
    snap = {"prior_passing": {"tests/test_paper_twelve_modules.py": {"test_b": "def test_b():\n    pass", "TestA": "class TestA:\n    pass"}}}
    note = ea._kept_note(snap, "tests/test_paper_twelve_modules.py")
    assert note.startswith(" -- except these, which the engine keeps as they passed and puts back by name, so leave them out: ")
    assert "`TestA`, `test_b`" in note
    assert ea._kept_note(snap, "tests/other.py") == "" and ea._kept_note({}, "tests/x.py") == ""
    big = {"prior_passing": {"tests/t.py": {f"test_{i}": "x" for i in range(45)}}}
    assert "… (45 in all)" in ea._kept_note(big, "tests/t.py")
    import inspect
    src = inspect.getsource(ea.execute_next_node)
    ask = src[src.index("async def _ask_tests("):src.index("async def _check_tests(")]
    assert "_kept_note(node_snapshot, _path)" in ask                        # the whole-file ask names them
    assert "_splice_kept_tests(_answer, node_snapshot, node_key) if _answer and not only else _answer" in ask
    assert "output = _splice_kept_tests(output, node_snapshot, node_key)        # §17.1899 backstop" in src


def test_a_kept_test_brings_the_fixture_and_helpers_it_stands_on_where_the_new_file_lacks_them():
    """§17.1916 — live (API_ALPACA_AUDITF1 a3): six kept tests taking `patched`, spliced into a draft whose fixtures
    were `client` / `fake_client` -- "fixture 'patched' not found"."""
    from app.modules.engine_contracts import kept_test_sources, splice_kept
    old = """```python
# tests/test_x.py
import pytest


def _order_json():
    return {"id": "o1"}


@pytest.fixture
def base():
    return 1


@pytest.fixture
def patched(base):
    return {"n": base, "order": _order_json()}


def _unused():
    return 0


def test_api_error(patched):
    assert patched["order"]["id"] == "o1"


def test_failed(patched):
    assert False
```"""
    kept = kept_test_sources(old, ["tests/test_x.py::test_api_error"])
    assert sorted(kept["tests/test_x.py"]) == ["_order_json", "base", "patched", "test_api_error"]   # not _unused
    new = """```python
# tests/test_x.py
import pytest


def _order_json():
    return {"id": "NEW"}


@pytest.fixture
def client():
    return 2


def test_get_account(client):
    assert client == 2
```"""
    out, names = splice_kept(new, kept)
    assert names == ["tests/test_x.py::test_api_error"]
    assert "def patched(base):" in out and "def base():" in out and "def test_api_error(patched):" in out
    assert out.count("def _order_json") == 1 and '"NEW"' in out      # the new file's own helper wins
    import ast
    ast.parse(out.split("```python\n", 1)[1].rsplit("```", 1)[0])       # the spliced file is whole


def test_a_kept_test_whose_fixture_is_defined_nowhere_is_not_put_back():
    """§17.1920 — live (API_ALPACA_AUDITF1, 02:16 UTC): kept tests recorded before §17.1916 carried no fixture;
    spliced back into a draft without `patched`, 23 errors."""
    from app.modules.engine_contracts import splice_kept
    kept = {"tests/test_x.py": {
        "test_api_error": "async def test_api_error(patched):\n    assert patched",
        "test_param": "@pytest.mark.parametrize('n', [1, 2])\ndef test_param(n, monkeypatch, client):\n    assert n",
        "test_plain": "def test_plain(tmp_path):\n    assert tmp_path"}}
    new = "```python\n# tests/test_x.py\nimport pytest\n\n\n@pytest.fixture\ndef client():\n    return 1\n\n\ndef test_new(client):\n    assert client\n```"
    out, names = splice_kept(new, kept)
    assert names == ["tests/test_x.py::test_param", "tests/test_x.py::test_plain"]      # `patched`: defined nowhere
    assert "def test_api_error" not in out and "def test_param(n, monkeypatch, client)" in out
    out2, names2 = splice_kept(new, {"tests/test_x.py": {**kept["tests/test_x.py"],
                                                          "patched": "@pytest.fixture\ndef patched():\n    return 1"}})
    assert "tests/test_x.py::test_api_error" in names2 and "def patched():" in out2      # kept with it: put back



def test_a_kept_test_that_reads_a_helper_defined_nowhere_is_not_put_back():
    """§17.1920 — live (API_ALPACA_AUDITF3 a1): kept tests calling `_mock_websocket`, recorded without it."""
    from app.modules.engine_contracts import splice_kept
    kept = {"tests/test_x.py": {
        "test_ws": "async def test_ws():\n    ws = _mock_websocket()\n    assert ws",
        "test_ok": "def test_ok():\n    assert _helper() == 1"}}
    new = "```python\n# tests/test_x.py\ndef _helper():\n    return 1\n\n\ndef test_new():\n    assert _helper()\n```"
    out, names = splice_kept(new, kept)
    assert names == ["tests/test_x.py::test_ok"] and "_mock_websocket" not in out


def test_a_kept_test_a_gate_refuses_is_dropped_and_the_drafts_own_are_not():
    """§17.1922 — live (API_FINRA_AUDITF1): nine kept tests still passing `'short_interest'` after the fix renamed the
    source values; refused before the sandbox, never un-kept, every attempt the same way."""
    import inspect

    from app.modules import execution_agent as ea
    from app.modules.engine_contracts import drop_refused_kept
    out = """```python
# tests/test_x.py
import pytest


def test_old_source():
    assert ingest(source="short_interest")


def test_new(client):
    assert ingest(source="finra_short_interest")


class TestKept:
    def test_a(self):
        assert ingest(source="short_interest")
```"""
    kept = {"tests/test_x.py": {"test_old_source": "", "TestKept": "", "_helper": ""}}
    findings = ["`tests/test_x.py:6` calls `ingest(…)`: `source` is one of …",
                "`tests/test_x.py:10` calls `ingest(…)`: the draft's own test",
                "`tests/test_x.py:15` calls `ingest(…)`: inside the kept class"]
    got, dropped = drop_refused_kept(out, kept, findings)
    assert dropped == ["tests/test_x.py::TestKept", "tests/test_x.py::test_old_source"]
    assert "def test_old_source" not in got and "class TestKept" not in got and "def test_new(client):" in got
    assert drop_refused_kept(out, {}, findings) == (out, [])                       # nothing kept: nothing dropped
    src = inspect.getsource(ea)
    assert src.index("await _drop_refused_kept_tests(output, _refs, _test_syntax") < src.index(
        "_ec.findings_in_tests(_refs) == _refs")                                    # before the findings are acted on
