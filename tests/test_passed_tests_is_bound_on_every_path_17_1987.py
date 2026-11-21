"""§17.1987 — `execute_next_node` binds `_passed_tests` before its verify branches.

It was bound only inside the codegen verify branch (§17.1891), while the `-> failed` log reads it on every path: the
self-blocked / instructions-only / asked-anyway / skip-verify branches raised UnboundLocalError there (all 7 of
tests/integration/test_codegen_golden_live.py on main 8747f631)."""
import inspect

from app.modules import execution_agent as ea


def test_it_is_bound_before_the_first_verify_branch():
    src = inspect.getsource(ea.execute_next_node)
    bound = src.index("_passed_tests: dict = {}")
    assert bound < src.index("    if _self_blocked:")
    # and the log that reads it comes after both
    assert bound < src.index('{"passed_tests": _passed_tests}')
