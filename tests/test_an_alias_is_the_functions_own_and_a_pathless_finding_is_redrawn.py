"""§17.1888 — an `app.modules` alias a function imports for itself names THAT module within the function, in every
gate that resolves one (patch targets, patch sites, sibling calls, whole-library patches); a finding with no
`tests/…` path falls back to the output's test file instead of ending the redraw; a NUMERIC column read back as text
keeps its scale (a sandbox cause).

Live (follow-up 5b4880c0 T5, 2026-10-31, the §17.1887 round, 11:39–11:52 UTC): a4's tests each did `from app.modules
import paper_x as mod` inside their own body -- five tests, five modules, one alias; the file-wide maps kept the
last, and `mod.request_approval(…)` was "`paper_order_executor` has no `request_approval`" (six of them), the patches
"`values_for` on `paper_order_executor`, which has no such name". a2's and a4's findings (`from app.modules.m import
x`: m has no x; `mod.f(…)`) carried no `tests/…` path -- two files to the redraw, no ask at all, both attempts spent.
a3: `assert '2.000000' == '2'` on three writer tests."""
from __future__ import annotations

import asyncio

from app.modules import engine_source as es
from app.modules.engine_contracts import (_patch_targets, _whole_library_patches, redraw_faked_tests,
                                          unknown_patch_targets, wrong_fakes)
from app.sandbox import engine_tests as et

PATH = "tests/test_paper_twelve_modules.py"

EXECUTOR = '''# app/modules/paper_order_executor.py
from app.database import async_session

async def execute_order(approval: dict) -> int:
    return 1
'''

GATE = '''# app/modules/paper_operator_approval_gate.py
from app.modules.runner_secrets import values_for

async def request_approval(proposal: dict, *, db=None) -> dict:
    return {"ok": True}
'''

REDDIT = '''# app/modules/paper_reddit_ingestion.py
import httpx

async def ingest_reddit_posts() -> int:
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get("https://x")
        except (httpx.HTTPError, httpx.TimeoutException):
            return 0
        return len(response.json())
'''

MODULES = {"paper_order_executor": EXECUTOR, "paper_operator_approval_gate": GATE, "paper_reddit_ingestion": REDDIT}


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


TEST = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules import paper_order_executor as mod

async def test_execute():
    await mod.execute_order({"approval_id": 1})

async def test_request_approval():
    from app.modules import paper_operator_approval_gate as mod
    with patch.object(mod, "values_for", new=AsyncMock(return_value={})):
        await mod.request_approval({"proposal_id": 1})

async def test_reddit():
    from app.modules import paper_reddit_ingestion as mod
    with patch.object(mod, "httpx") as fake_httpx:
        fake_httpx.AsyncClient = MagicMock()
        await mod.ingest_reddit_posts()
'''


def test_an_alias_a_function_imports_for_itself_names_that_module_in_every_gate():
    assert _patch_targets(TEST) == [("paper_operator_approval_gate", "values_for", 10), ("paper_reddit_ingestion", "httpx", 15)]
    assert [f for f in unknown_patch_targets({PATH: TEST}, MODULES, set()) if "has no such name" in f] == []
    defs = es.sibling_definitions([_row("T2", EXECUTOR), _row("T16", GATE), _row("T12", REDDIT)])
    assert [f for f in es.sibling_refs(f"```python\n{TEST}```", defs) if "has no" in f] == []
    whole = _whole_library_patches(PATH, TEST, MODULES)
    assert len(whole) == 1 and whole[0].startswith("`tests/test_paper_twelve_modules.py:15` patches `httpx` whole on `paper_reddit_ingestion`"), whole
    # the file-wide alias still rules where a function imports nothing of its own
    assert [f for f in es.sibling_refs("```python\n" + TEST.replace("await mod.execute_order({\"approval_id\": 1})",
                                                                       "await mod.request_approval({})") + "```", defs) if "has no" in f] == [
        "`mod.request_approval(…)`: `paper_order_executor` (step T2) has no `request_approval`"]
    # and the fakes gate reads the patch through the function's own alias: a MagicMock where the module awaits
    got = wrong_fakes({PATH: TEST}, MODULES, set())
    assert len(got) == 1 and got[0].startswith("`tests/test_paper_twelve_modules.py:15` patches `httpx` on `paper_reddit_ingestion` and sets `.AsyncClient`"), got


def test_a_finding_without_a_test_path_falls_back_to_the_output_s_test_file():
    output = "```python\n# tests/test_paper_twelve_modules.py\nfrom app.modules.paper_order_executor import write_order\n\ndef test_a():\n    assert write_order\n```"
    asked: list[tuple[str, list[str] | None]] = []

    async def ask(path, refusal, current, only=None):
        asked.append((path, only))
        return "```python\n# tests/test_paper_twelve_modules.py\nfrom app.modules.paper_order_executor import execute_order\n\ndef test_a():\n    assert execute_order\n```"

    async def check(candidate):
        return [] if "execute_order" in candidate else ["`from app.modules.paper_order_executor import write_order`: `paper_order_executor` (step T2) has no `write_order`"]

    out, left = asyncio.run(redraw_faked_tests(
        output, ["`from app.modules.paper_order_executor import write_order`: `paper_order_executor` (step T2) has no `write_order`",
                 "`mod.run_backtest(…)`: `paper_order_executor` (step T2) has no `run_backtest`"],
        ask=ask, check=check, tries=4, tests_only=True))
    assert left == [] and "execute_order" in out
    assert asked == [("tests/test_paper_twelve_modules.py", None)]       # the whole file: no line to name a definition by


def test_a_numeric_column_read_back_as_text_keeps_its_scale():
    rx, hint = et.KNOWN_CAUSES[7]
    assert rx.search("E   AssertionError: assert '2.000000' == '2'")
    assert rx.search("E   AssertionError: assert '10000.00' == '10000'")
    assert not rx.search("E   AssertionError: assert '2.000000' == '3'")
    assert not rx.search("E   assert 'accepted' == 'submitted'")
    assert hint.startswith("a NUMERIC column read back as text keeps the column's scale")
