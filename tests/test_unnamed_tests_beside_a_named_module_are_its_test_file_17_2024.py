"""§17.2024 — an answer that names its module and writes its tests in an unnamed block: the tests are its test file.

Live (69fe61c4 NOKEY2 a4, 2026-11-24): the answer was `# app/modules/<leaf>.py` then the whole test file with no header.
`files_of` gives the first unnamed block the step's module path; the named block already held it, so the test file
was dropped (§17.1838). §17.1801 then found the required test file "left out" and drew it again, and the answer held
the test file twice -- the model's own version never read, a model call spent on a file it had written."""
from __future__ import annotations

from app.modules.engine_contracts import answer_with_its_module
from app.modules.engine_source import missing_test_file
from app.sandbox.engine_tests import files_of

LEAF = "paper_wire_engine_entry_module"
MOD, TESTS = f"app/modules/{LEAF}.py", f"tests/test_{LEAF}.py"
STEP = f"Make the dashboard answer without the Alpaca keys. Tests in `{TESTS}`."
MODULE = f'''```python
# {MOD}
from app.modules.runner_secrets import values_for


async def _load_spy(db):
    return await values_for(db, ["ALPACA_API_KEY_ID"])


def _spy_missing(values):
    return not values
```'''
#: the whole test file, unnamed: imports, a fixture, a helper and several tests
UNNAMED_TESTS = f'''```python
import pytest

from app.modules import {LEAF} as entry


@pytest.fixture
def no_keys(monkeypatch):
    async def _empty(db, names):
        return {{}}
    monkeypatch.setattr(entry, "values_for", _empty)


def _rows():
    return []


async def test_load_spy_without_keys(no_keys):
    assert await entry._load_spy(None) == {{}}


def test_spy_missing_true_for_empty():
    assert entry._spy_missing({{}})


def test_spy_missing_false_with_a_key():
    assert not entry._spy_missing({{"ALPACA_API_KEY_ID": "k"}})
```'''
LIVE = MODULE + "\n\n" + UNNAMED_TESTS


def _files(out: str) -> dict[str, str]:
    return files_of({"output_text": out, "tool": "CodeGen", "tool_config": {"fixes_module": LEAF}})


def test_the_live_shape_loses_the_tests_and_asks_for_them_again():
    # the detector bites: what the engine read before -- the module only, and the test file owed
    assert set(_files(LIVE)) == {MOD}
    assert missing_test_file(LIVE, STEP) == TESTS


def test_the_unnamed_tests_are_the_test_file_and_nothing_is_owed():
    out = answer_with_its_module(LIVE, "", LEAF)
    fs = _files(out)
    assert set(fs) == {MOD, TESTS}
    assert fs[MOD].strip() == MODULE.strip("`").removeprefix("python\n").strip()       # the module untouched
    for name in ("no_keys", "_rows", "test_load_spy_without_keys", "test_spy_missing_true_for_empty",
                 "test_spy_missing_false_with_a_key"):
        assert f"def {name}" in fs[TESTS]
    assert missing_test_file(out, STEP) is None                                         # no draw of a "left out" file


def test_a_named_test_file_stands_and_unnamed_code_is_left_alone():
    # the correct misses: the answer's named test file wins over an unnamed one; an unnamed block that is code is
    # not tests
    named = f"```python\n# {TESTS}\ndef test_named():\n    assert True\n```"
    fs = _files(answer_with_its_module(LIVE + "\n\n" + named, "", LEAF))
    assert "def test_named" in fs[TESTS] and "test_spy_missing_true_for_empty" not in fs[TESTS]
    helper = MODULE + "\n\n```python\ndef helper():\n    return 1\n```"
    assert answer_with_its_module(helper, "", LEAF) == helper
