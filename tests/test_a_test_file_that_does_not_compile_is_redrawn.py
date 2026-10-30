"""§17.1869 — a test file that does not compile is a test-file finding: the file is asked for again, the attempt is
not spent. Live (5b4880c0 T5, 2026-10-30 01:31 UTC): the last attempt's 72-test file had `async def def test_…` on
line 555 of 1629, and the §17.428 refusal took the attempt. And the sandbox report names every failure: 50 of 70
failed in attempt 2 and the stored refusal carried six."""
from __future__ import annotations

import inspect

from app.modules import engine_source as es
from app.modules import execution_agent as xa
from app.sandbox import engine_tests as et

GOOD = "```python\n# app/modules/paper_x.py\nX = 1\n```\n"
BAD_TEST = "```python\n# tests/test_paper_twelve_modules.py\nimport pytest\n\nasync def def test_it():\n    pass\n```\n"
BAD_MODULE = "```python\n# app/modules/paper_x.py\ndef def broken():\n    pass\n```\n"


def test_a_syntax_error_in_the_test_block_is_a_test_file_finding_and_one_in_the_module_is_not():
    assert es.syntax_only_in_tests(GOOD + BAD_TEST) is True
    assert es.test_syntax_findings(GOOD + BAD_TEST) == [
        "`tests/test_paper_twelve_modules.py:4` does not compile: invalid syntax (`async def def test_it():`) -- fix that "
        "line; the file must compile"]
    assert es.syntax_only_in_tests(BAD_MODULE + BAD_TEST) is False and es.syntax_only_in_tests(GOOD) is False
    assert es.test_syntax_findings(BAD_MODULE) == []
    # and the §17.1864 keep-list does not also call the file empty: one finding for the typo, not two
    from app.modules.engine_contracts import dropped_tests
    assert dropped_tests(GOOD + BAD_TEST, {"tests/test_paper_twelve_modules.py": ["test_it", "test_other"]}) == []
    assert dropped_tests(GOOD, {"tests/test_paper_twelve_modules.py": ["test_it"]})            # absent: still dropped


def test_the_executor_routes_a_test_only_syntax_error_to_the_redraw():
    src = inspect.getsource(xa.execute_next_node)
    assert "_es_syn.syntax_only_in_tests(output)" in src and "_test_syntax = _es_syn.test_syntax_findings(output)" in src
    assert "_refs = _test_syntax + await _es.engine_findings(_esdb, job_id, node_key, output)" in src
    # and the redraw's own check compiles the candidate too, so a redraw that still does not parse is not taken
    assert "_es.test_syntax_findings(_cand) + await _es.engine_findings(_cdb, job_id, node_key, _cand)" in src
    assert 'return ["the module block does not compile"]' in src


def test_the_sandbox_report_names_every_failure_when_the_budget_cuts():
    body = []
    for i in range(50):
        body += [f"__________________________ test_case_{i:02d} __________________________",
                 f"tests/test_x.py:{10 + i}: in test_case_{i:02d}",
                 "    await thing()",
                 f"E   AttributeError: 'coroutine' object has no attribute 'request' ({i})",
                 "E   DETAIL:  more"]
    text = "\n".join(["F" * 50 + "  [100%]", "=== FAILURES ===", *body, "=== short test summary info ===",
                      *[f"FAILED tests/test_x.py::test_case_{i:02d}" for i in range(50)], "50 failed in 9.0s"])
    out = et._report(text)
    assert out.startswith("Every failure, one line each")
    assert "test_case_49: E   AttributeError: 'coroutine' object has no attribute 'request' (49)" in out
    assert out.count("test_case_") >= 60 and len(out) <= 9200          # the index, then the first tracebacks
    assert "50 failed in 9.0s" in out
    assert et._report(text[:3000]) == "\n".join(ln for ln in text[:3000].splitlines() if ln.strip())   # small: whole
