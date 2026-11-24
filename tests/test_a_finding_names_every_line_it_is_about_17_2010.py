"""§17.2010 — a patch of a name the module does not have is refused at every line that makes it, not the first.

Live (69fe61c4 NOKEY2, 2026-11-24): five tests patched `values_for` on the entry module. Each refusal named one line
(799, 763, 787, 757, 834); the retry is told to keep everything its findings do not name, so each attempt fixed that
line and kept the other four, and the step failed."""
from __future__ import annotations

from app.modules.engine_contracts import unknown_patch_targets

MOD = "lab_entry"
CODE = '''LAB = 1


def load():
    return LAB
'''
PATH = f"tests/test_{MOD}.py"


def _tests(n: int) -> str:
    head = f"from unittest.mock import AsyncMock, patch\n\nfrom app.modules import {MOD} as entry\n"
    return head + "".join(f'''

def test_{i}():
    with patch.object(entry, "values_for", AsyncMock(return_value={{}})):
        assert entry.load() == 1
''' for i in range(n))


def _found(n: int) -> list[str]:
    return [f for f in unknown_patch_targets({PATH: _tests(n)}, {MOD: CODE}, {MOD}) if "`values_for`" in f]


def test_every_line_that_patches_the_missing_name_is_named():
    found = _found(5)
    assert len(found) == 1, found
    lines = [i + 1 for i, ln in enumerate(_tests(5).splitlines()) if '"values_for"' in ln]
    assert len(lines) == 5
    assert found[0].startswith(f"`{PATH}:{lines[0]}` (and lines {', '.join(map(str, lines[1:]))}) patches `values_for` "
                               f"on `{MOD}`, which has no such name"), found[0]


def test_one_line_reads_as_it_did():
    # the correct miss: a single patch keeps the plain form
    found = _found(1)
    assert len(found) == 1 and found[0].startswith(f"`{PATH}:7` patches `values_for` on `{MOD}`"), found
