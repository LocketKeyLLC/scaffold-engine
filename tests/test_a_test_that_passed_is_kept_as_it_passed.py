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
        "test_c": "@pytest.mark.asyncio\nasync def test_c():\n    assert is_trade_update(_envelope()) is True"}}
    # every method of the class passed: the class is kept whole
    both = kept_test_sources(OUTPUT, ["tests/test_paper_twelve_modules.py::TestK::test_a",
                                      "tests/test_paper_twelve_modules.py::TestK::test_b"])
    assert set(both["tests/test_paper_twelve_modules.py"]) == {"TestK"}
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


async def test_the_latest_verdict_of_each_name_wins_over_the_same_window_as_the_keep_list():
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
    assert "passed_tests" in db.sql and "e.op IN ('replan', 'restore', 'reset')" in db.sql


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
