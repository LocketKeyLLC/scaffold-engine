"""§17.2013 — a retry's answer that holds only tests is read as tests, and keeps the module of the draft it fixes.

Live (69fe61c4 NOKEY2 a2, 2026-11-24 16:20 UTC): the prompt says the tests that passed are put back by the engine; one
test failing of 62, the model answered only that test, unnamed, with no module. `files_of` read the unnamed block as
the step's module -- `paper_wire_engine_entry_module` became one test function -- and the next attempt rewrote the
module from nothing: 40 failed, 10 errors. Replayed: on main `files_of` gave the module 262 chars of test; now the test
file holds the test and the module is the draft's (17,149 chars)."""
from __future__ import annotations

from app.modules.engine_contracts import answer_with_its_module
from app.sandbox.engine_tests import files_of

LEAF = "lab_entry"
MOD, TESTS = f"app/modules/{LEAF}.py", f"tests/test_{LEAF}.py"
MODULE = f'''# {MOD}
def load():
    return 2
'''
PRIOR = f"```python\n{MODULE}```\n\n```python\n# {TESTS}\nfrom app.modules.{LEAF} import load\n\n\ndef test_load():\n    assert load() == 2\n```"
ONE_TEST = '''```python
@pytest.mark.asyncio
async def test_load_returns_two():
    assert load() == 2
```'''


def _files(out: str) -> dict[str, str]:
    return files_of({"output_text": out, "tool": "CodeGen", "tool_config": {"fixes_module": LEAF}})


def test_the_live_shape_on_its_own_makes_the_test_the_module():
    # the detector bites: what the engine read before
    assert "def test_load_returns_two" in _files(ONE_TEST)[MOD]


def test_one_unnamed_test_is_the_test_file_and_the_module_is_the_drafts():
    fs = _files(answer_with_its_module(ONE_TEST, PRIOR, LEAF))
    assert "def test_load_returns_two" in fs[TESTS] and fs[TESTS].startswith(f"# {TESTS}")
    assert fs[MOD].strip() == MODULE.strip()


def test_an_unnamed_test_beside_the_named_test_file_is_dropped():
    named = f"```python\n# {TESTS}\ndef test_load():\n    assert True\n```"
    fs = _files(answer_with_its_module(ONE_TEST + "\n\n" + named, PRIOR, LEAF))
    assert "test_load_returns_two" not in fs[TESTS] and fs[MOD].strip() == MODULE.strip()


def test_an_answer_with_a_module_of_its_own_is_left_as_it_is():
    # the correct misses: its own module (named or unnamed) stands, and a non-test unnamed block is code.
    # §17.2024 — beside a named module, its unnamed tests become its test file (they were dropped before)
    own = f"```python\n# {MOD}\ndef load():\n    return 3\n```\n\n" + ONE_TEST
    fs = _files(answer_with_its_module(own, PRIOR, LEAF))
    assert "return 3" in fs[MOD] and "def test_load_returns_two" in fs[TESTS]
    plain = "```python\ndef load():\n    return 3\n```"
    assert answer_with_its_module(plain, PRIOR, LEAF) == plain


def test_a_draft_cut_short_carries_no_module():
    cut = PRIOR.replace("    return 2\n", "    return (\n… [§17.1974: 900 chars cut] …\n", 1)
    fs = _files(answer_with_its_module(ONE_TEST, cut, LEAF))
    assert MOD not in fs and "test_load_returns_two" in fs[TESTS]
