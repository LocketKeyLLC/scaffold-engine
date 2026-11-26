"""§17.2021 — a kept test a gate refuses is dropped wherever the finding names it, not only at its leading line.

§17.1922 drops a kept test (§17.1891) that a pre-sandbox gate refuses, so the engine never re-injects what its own
gate turns down. It read only the finding's leading `path:line`. Since §17.2010 a finding names every line it is about
("`path:L1` (and lines L2, …)"); live (69fe61c4 NOKEY2, 2026-11-24) one finding named six kept tests, one was
dropped, and the other five were spliced back into every draw the same gate then refused -- the deadlock §17.2011
closed for patch targets only, by keeping them out of the splice. Dropping at every named line closes it for any
gate that names the lines."""
from __future__ import annotations

from app.modules.engine_contracts import _lines_named, drop_refused_kept, unknown_patch_targets
from app.sandbox.engine_tests import files_of

MOD = "lab_entry"
PATH = f"tests/test_{MOD}.py"
MODULE = f'''# app/modules/{MOD}.py
LAB = 1


def load():
    return LAB
'''
KEPT = {
    "test_load_a": '''def test_load_a():
    with patch.object(entry, "values_for", AsyncMock(return_value={})):
        assert entry.load() == 1
''',
    "test_load_b": '''def test_load_b():
    with patch.object(entry, "values_for", AsyncMock(return_value={})):
        assert entry.load() == 1
''',
    "test_load_c": '''def test_load_c():
    with patch.object(entry, "values_for", AsyncMock(return_value={})):
        assert entry.load() == 1
''',
    "test_lab": '''def test_lab():
    with patch.object(entry, "LAB", 1):
        assert entry.LAB == 1
''',
}
#: the answer's own test, also patching the missing name: not kept, so the attempt answers for it
OWN = '''def test_own():
    with patch.object(entry, "values_for", AsyncMock(return_value={})):
        assert entry.load() == 1
'''
TESTS = (f"# {PATH}\nfrom unittest.mock import AsyncMock, patch\n\nfrom app.modules import {MOD} as entry\n\n\n"
         + "\n\n".join(KEPT.values()) + "\n\n" + OWN)


def _output() -> str:
    return f"```python\n{MODULE}```\n\n```python\n{TESTS}```"


def _findings(output: str) -> list[str]:
    return unknown_patch_targets(files_of({"output_text": output}), {}, {MOD})


def test_the_gate_names_every_kept_line_in_one_finding():
    found = _findings(_output())
    assert len(found) == 1 and "(and lines " in found[0]
    assert len(_lines_named(found[0])) == 4                     # three kept tests and the answer's own


def test_every_kept_test_the_finding_names_is_dropped():
    out, gone = drop_refused_kept(_output(), {PATH: KEPT}, _findings(_output()))
    assert gone == [f"{PATH}::test_load_a", f"{PATH}::test_load_b", f"{PATH}::test_load_c"]
    code = files_of({"output_text": out})[PATH]
    assert "def test_lab" in code                               # the correct miss: a kept test no finding names
    assert "def test_own" in code                               # and the answer's own test is the attempt's to fix
    assert _findings(out) and "(and line" not in _findings(out)[0]   # only the answer's own line is left


def test_lines_named_reads_both_shapes_and_nothing_else():
    assert _lines_named("`tests/x.py:7` patches `n` on `m`") == {7}
    assert _lines_named("`tests/x.py:7` (and line 9) patches") == {7, 9}
    assert _lines_named("`tests/x.py:7` (and lines 9, 12, 30) patches") == {7, 9, 12, 30}
    assert _lines_named("`tests/x.py` dropped 3 of its tests") == set()
