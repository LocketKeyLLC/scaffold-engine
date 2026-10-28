"""§17.1855 — a test is shown the only values a function accepts, and a literal outside them is refused before the sandbox.

Live (follow-up 5b4880c0 T5 "Write tests for twelve modules", 2026-10-28): `ingest_finra_file` refuses
`source not in PARSERS` -- `short_interest`, `ats_volume` -- and three attempts' tests passed `source="test"`, then
`"finra"`, each costing a sandbox run ("ValueError: unknown FINRA source 'test'"). The step was shown the signature
and the docstring, never the values the function itself refuses. Now the call shape carries `values` read from the
function's own refusal (`if p not in X: raise …`, `assert p in X`; X a literal collection of strings, its `.keys()`,
or a module constant bound to one), the signature shows them (`source:short_interest|ats_volume`), the contracts
block says them first, and a call with a literal outside them is refused by the names gate.
"""
from __future__ import annotations

import ast

from app.modules import engine_source as es
from app.modules.engine_contracts import _said, contracts_block

FINRA = '''# app/modules/paper_finra_data_ingestion.py
from typing import Any


def _parse_short_interest(content, release_date):
    return []


def _parse_ats_volume(content, release_date):
    return []


PARSERS: dict[str, Any] = {
    "short_interest": _parse_short_interest,
    "ats_volume": _parse_ats_volume,
}
SIDES = frozenset({"buy", "sell"})
MIXED = {"a": 1, 2: 3}
EMPTY: dict[str, int] = {}


async def ingest_finra_file(source: str, *, db, client, version=1):
    """Ingest one FINRA file into the audit trail."""
    if source not in PARSERS:
        raise ValueError(f"unknown FINRA source {source!r}")
    return {"source": source}


async def ingest_finra_files(*, source, release_dates, db, client):
    """Ingest several files."""
    return [await ingest_finra_file(source, db=db, client=client) for _ in release_dates]


def side_of(side):
    assert side in SIDES
    return side


def by_keys(kind):
    if kind not in PARSERS.keys():
        raise KeyError(kind)


def inline(mode: str):
    """Run in a mode."""
    if mode not in ("fast", "slow"):
        raise ValueError(mode)


def logged(level):
    """Not a refusal: the function carries on."""
    if level not in ("a", "b"):
        return None
    return level


def unknown(x):
    if x not in EMPTY:
        raise KeyError(x)
'''
ROW = {"node_key": "T4", "status": "done", "title": "FINRA ingestion", "prompt_template": "",
       "output_text": f"```python\n{FINRA}```"}
SIG = "async ingest_finra_file(source:short_interest|ats_volume, *, db, client, version=…)"


def _step(call: str) -> str:
    return ('```python\n# app/modules/paper_finra_runner.py\n'
            'from app.modules.paper_finra_data_ingestion import ingest_finra_file\n\nSRC = "nope"\n\n'
            f'async def run(db, client):\n    return await {call}\n```')


def test_the_values_a_function_refuses_others_for_are_read_from_its_own_check():
    tree = ast.parse(FINRA)
    assert es.module_values(tree) == {"PARSERS": ["short_interest", "ats_volume"], "SIDES": ["buy", "sell"]}
    funcs = es.sibling_definitions([ROW])["paper_finra_data_ingestion"]["funcs"]
    assert funcs["ingest_finra_file"]["values"] == {"source": ["short_interest", "ats_volume"]}
    assert funcs["ingest_finra_files"]["values"] == {"source": ["short_interest", "ats_volume"]}   # handed on
    assert funcs["side_of"]["values"] == {"side": ["buy", "sell"]}                    # assert p in X
    assert funcs["by_keys"]["values"] == {"kind": ["short_interest", "ats_volume"]}   # X.keys()
    assert funcs["inline"]["values"] == {"mode": ["fast", "slow"]}                    # an inline tuple
    assert funcs["logged"]["values"] == {}                                             # no raise: no refusal
    assert funcs["unknown"]["values"] == {}                                            # an empty collection
    assert es._shape_signature("ingest_finra_file", funcs["ingest_finra_file"]) == SIG
    assert es._shape_signature("inline", funcs["inline"]) == "inline(mode:fast|slow)"


def test_a_literal_the_function_refuses_is_refused_before_the_sandbox():
    defs = es.sibling_definitions([ROW])
    found = es.sibling_refs(_step('ingest_finra_file(source="test", db=db, client=client)'), defs)
    assert found == [f"`ingest_finra_file(…)`: `source` is one of `short_interest`, `ats_volume`; it was given `'test'` "
                     f"-- the signature is `{SIG}` (`paper_finra_data_ingestion`, step T4)"], found
    assert es.sibling_refs(_step('ingest_finra_file(source="ats_volume", db=db, client=client)'), defs) == []
    assert es.sibling_refs(_step('ingest_finra_file("short_interest", db=db, client=client)'), defs) == []
    assert es.sibling_refs(_step('ingest_finra_file(source=client.source, db=db, client=client)'), defs) == []
    # a test file's call is checked the same way (live: the step's tests, not its module, made the call)
    test = ('```python\n# tests/test_paper_finra_runner.py\n'
            'from app.modules.paper_finra_data_ingestion import ingest_finra_file\n\n'
            'async def test_x(db, client):\n    await ingest_finra_file(source="finra", db=db, client=client)\n```')
    assert any("`source` is one of `short_interest`, `ats_volume`; it was given `'finra'`" in f
               for f in es.sibling_refs(test, defs))


def test_the_contract_line_says_the_values_first_and_a_silent_function_still_gets_one():
    said = dict(_said(FINRA))
    assert said["ingest_finra_file(source: str, *, db, client, version=1)"] == \
        "`source` is one of `short_interest`, `ats_volume` -- Ingest one FINRA file into the audit trail."
    assert said["side_of(side)"] == "`side` is one of `buy`, `sell`"                    # no docstring, still said
    assert said["ingest_finra_files(*, source, release_dates, db, client)"] == \
        "`source` is one of `short_interest`, `ats_volume` -- Ingest several files."
    assert said["logged(level)"] == "Not a refusal: the function carries on."
    block = contracts_block("Write tests for paper_finra_data_ingestion.", {"paper_tests"},
                            {"paper_finra_data_ingestion": FINRA}, per=64)
    line = next(ln for ln in block.splitlines() if ".ingest_finra_file(" in ln)
    assert line.startswith("- `paper_finra_data_ingestion.ingest_finra_file(source: str, *, db, client, version=1)`: "
                           "`source` is one of `short_interest`, `ats_volume` -- Ingest") and line.endswith(" …")
    # the values come first, so the cut at `per` takes the docstring's tail, never them


def test_the_engines_own_source_passes_its_own_value_check():
    from pathlib import Path
    bad = [(p.name, f[:120]) for p in sorted(es.ENGINE_PKG.rglob("*.py"))
           for f in es.unresolved_refs("```python\n" + Path(p).read_text() + "\n```") if " is one of `" in f]
    assert bad == [], bad
