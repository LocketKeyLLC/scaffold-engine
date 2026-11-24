"""§17.2011 — a test kept from an earlier attempt is not put back when it patches a name that a module this answer
writes no longer has.

Live (69fe61c4 NOKEY2, 2026-11-24): tests that passed under a draft whose module read `values_for` itself were spliced
back (§17.1891) into every later draft with `patch.object(entry, "values_for", …)`. The patch-target gate refused each
answer before the sandbox ran, so no verdict ever took them off the keep-list. Replayed on the run log's real keep-list:
the step's own latest answer had none of these patches; after the splice it had six."""
from __future__ import annotations

from app.modules.engine_contracts import splice_kept, unknown_patch_targets
from app.sandbox.engine_tests import files_of

MOD = "lab_entry"
PATH = f"tests/test_{MOD}.py"
MODULE = f'''# app/modules/{MOD}.py
LAB = 1


def load():
    return LAB
'''
HEAD = f'''# {PATH}
from unittest.mock import AsyncMock, patch

from app.modules import {MOD} as entry
'''
NEW_TESTS = HEAD + '''

def test_load():
    assert entry.load() == 1


def test_lab():
    assert entry.LAB == 1
'''
#: what passed under the draft whose module had `values_for`
KEPT_BROKEN = '''def test_load():
    with patch.object(entry, "values_for", AsyncMock(return_value={})):
        assert entry.load() == 1
'''
KEPT_FINE = '''def test_lab():
    with patch.object(entry, "LAB", 1):
        assert entry.LAB == 1
'''


def _out(tests: str) -> str:
    return f"```python\n{MODULE}```\n\n```python\n{tests}```"


def _bad(output: str) -> list[str]:
    fs = files_of({"output_text": output})
    return unknown_patch_targets(fs, {}, {MOD})


def test_a_kept_test_that_patches_what_the_rewrite_lacks_is_not_spliced():
    out, names = splice_kept(_out(NEW_TESTS), {PATH: {"test_load": KEPT_BROKEN, "test_lab": KEPT_FINE}})
    assert f"{PATH}::test_load" not in names
    assert f"{PATH}::test_lab" in names                       # the correct miss: a kept test that still fits goes in
    assert 'patch.object(entry, "LAB", 1)' in out
    assert '"values_for"' not in out
    assert _bad(out) == []


def test_without_the_check_the_splice_brought_the_patch_back():
    # the detector bites: the kept text alone, spliced, is what the gate refused
    out = _out(NEW_TESTS.replace('''def test_load():
    assert entry.load() == 1
''', KEPT_BROKEN))
    assert _bad(out)


def test_a_kept_test_whose_patch_target_the_module_has_is_spliced_as_before():
    module_with = MODULE + "\n\nasync def values_for(db, names):\n    return {}\n"
    out, names = splice_kept(f"```python\n{module_with}```\n\n```python\n{NEW_TESTS}```",
                             {PATH: {"test_load": KEPT_BROKEN}})
    assert f"{PATH}::test_load" in names and '"values_for"' in out
