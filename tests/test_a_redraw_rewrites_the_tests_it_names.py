"""§17.1877 — a redraw rewrites the definitions the findings name, spliced in by name; a pass is counted by its
progress; an answer identical to the file ends the loop.

Live (follow-up 5b4880c0 T5, 2026-10-30, the §17.1876 round, 22:01–22:18 UTC): every attempt was spent before the
sandbox on a handful of correct findings the two whole-file redraws left behind -- 1 (a syntax error) -> 69 -> 5;
56 -> 5; 120 -> 6; 109 -> 2 -> 2. Read from the saved outputs: attempt 1's first redraw changed 8 lines (the brace),
its second 1,935 of 1,629 and dropped two tests; attempt 4's two redraws returned byte-identical 98-test files.
Each pass cost ~16k completion tokens and a minute."""
from __future__ import annotations

import inspect

import pytest

from app.modules import execution_agent as ea
from app.modules.engine_contracts import (_finding_lines, definitions_at, redraw_faked_tests, splice_definitions)

FILE = '''# tests/test_paper_twelve_modules.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.modules.paper_congressional_trade_ingestion import fetch_disclosures


def _fake_client(payload):
    response = MagicMock()
    response.json = AsyncMock(return_value=payload)
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_fetch_disclosures_parses_rows():
    client = _fake_client({"disclosures": []})
    result = await fetch_disclosures(client, max_requests=3)
    assert result.requests_made == 0


class TestOther:
    def test_untouched(self):
        assert 1 + 1 == 2


async def test_last():
    assert True
'''

OUT = "```python\n# app/modules/paper_congressional_trade_ingestion.py\nX = 1\n```\n\n```python\n" + FILE + "```\n"
PATH = "tests/test_paper_twelve_modules.py"
F_USE = ("`tests/test_paper_twelve_modules.py:19` hands `fetch_disclosures(…)` `client` = a fake whose `get()` returns a "
         "fake whose `json()` returns a coroutine (line 10): an `AsyncMock` method called without `await`")
F_DROP = "`tests/test_paper_twelve_modules.py` dropped 1 of the 2 tests your earlier attempts wrote: `test_gone`"


def test_the_lines_a_finding_names_map_to_the_definitions_that_hold_them():
    from app.modules.engine_contracts import _dropped_names
    assert _finding_lines([F_USE, "`tests/test_x.py:3` x", "no line here"]) == [3, 10, 19]
    assert _dropped_names(F_DROP + " -- keep every test by name") == ["test_gone"] and _dropped_names(F_USE) == []
    assert definitions_at(FILE, [10, 19]) == ["_fake_client", "test_fetch_disclosures_parses_rows"]
    assert definitions_at(FILE, [16]) == ["test_fetch_disclosures_parses_rows"]          # the decorator line
    assert definitions_at(FILE, [24]) == ["TestOther"]                                   # a method: its class
    assert definitions_at(FILE, [2, 400]) == [] and definitions_at("x = (", [1]) == []


def test_definitions_are_spliced_in_by_name_and_the_rest_stays_byte_for_byte():
    new = '''```python
# tests/test_paper_twelve_modules.py
import json


def _fake_client(payload):
    response = MagicMock()
    response.json = MagicMock(return_value=payload)
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
@pytest.mark.slow
async def test_fetch_disclosures_parses_rows():
    client = _fake_client({"results": [{"a": 1}]})
    result = await fetch_disclosures(client, max_requests=3)
    assert len(result.rows) == 1


def _new_helper():
    return json.dumps({})
```'''
    out = splice_definitions(FILE, new)
    assert out is not None
    assert "response.json = MagicMock(return_value=payload)" in out and "response.json = AsyncMock" not in out
    assert "@pytest.mark.slow\nasync def test_fetch_disclosures_parses_rows" in out
    assert out.count("def test_fetch_disclosures_parses_rows") == 1 and out.count("def _fake_client") == 1
    assert "class TestOther:\n    def test_untouched(self):\n        assert 1 + 1 == 2\n" in out
    assert out.rstrip().endswith("def _new_helper():\n    return json.dumps({})")       # a new name: appended
    assert out.splitlines()[:5] == ["# tests/test_paper_twelve_modules.py", "import pytest",
                                    "from unittest.mock import AsyncMock, MagicMock", "",
                                    "from app.modules.paper_congressional_trade_ingestion import fetch_disclosures"]
    assert "import json" in out and out.index("import json") < out.index("def _fake_client")   # after the imports
    # the untouched lines are exactly the original's
    for ln in FILE.splitlines()[1:6] + FILE.splitlines()[22:]:
        assert ln in out.splitlines()
    # a class redrawn whole; nothing defined, or an answer that does not parse: None
    cls = splice_definitions(FILE, "class TestOther:\n    def test_untouched(self):\n        assert 2 + 2 == 4\n")
    assert cls is not None and "assert 2 + 2 == 4" in cls and "assert 1 + 1 == 2" not in cls
    assert splice_definitions(FILE, "x = 1\n") is None and splice_definitions(FILE, "def broken(:\n") is None
    assert splice_definitions(FILE, "") is None


@pytest.mark.asyncio
async def test_a_redraw_asks_for_the_named_definitions_and_splices_them_in():
    asked: list[tuple[str, list[str] | None]] = []
    fixed = '''def _fake_client(payload):
    response = MagicMock()
    response.json = MagicMock(return_value=payload)
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    return client
'''

    async def ask(path, refusal, current, only=None):
        asked.append((path, only))
        return f"```python\n{fixed}```"

    async def check(candidate):
        return [] if "response.json = MagicMock" in candidate else [F_USE]

    out, left = await redraw_faked_tests(OUT, [F_USE], ask=ask, check=check, tests_only=True)
    assert left == [] and asked == [(PATH, ["_fake_client", "test_fetch_disclosures_parses_rows"])]
    assert "response.json = MagicMock(return_value=payload)" in out
    assert "class TestOther:\n    def test_untouched(self):" in out and "async def test_last():" in out
    assert out.startswith("```python\n# app/modules/paper_congressional_trade_ingestion.py\nX = 1\n```")
    # a dropped test (no line) is a definition to write anew: named with the rest, appended by the splice
    asked.clear()

    async def gone(path, refusal, current, only=None):
        asked.append((path, only))
        return "```python\nasync def test_gone():\n    assert True\n```"

    out, left = await redraw_faked_tests(OUT, [F_USE, F_DROP], ask=gone, check=lambda c: _aret([]), tests_only=True)
    assert left == [] and asked == [(PATH, ["_fake_client", "test_fetch_disclosures_parses_rows", "test_gone"])]
    assert "async def test_gone():\n    assert True" in out and "async def test_last():" in out
    # any other finding without a line, or findings over most of the file: the whole file is asked for, as before
    asked.clear()

    async def whole(path, refusal, current, only=None):
        asked.append((path, only))
        return f"```python\n{FILE}async def test_gone():\n    assert True\n```"

    sql = "`tests/test_paper_twelve_modules.py` runs `SELECT ticker FROM fill`, and `fill` has no `ticker`"
    out, left = await redraw_faked_tests(OUT, [F_USE, sql], ask=whole, check=lambda c: _aret([]), tests_only=True)
    assert left == [] and asked == [(PATH, None)] and "def test_gone" in out
    asked.clear()
    many = [F_USE, "`tests/test_paper_twelve_modules.py:24` x", "`tests/test_paper_twelve_modules.py:28` y"]
    out, left = await redraw_faked_tests(OUT, many, ask=whole, check=lambda c: _aret([]), tests_only=True)
    assert asked == [(PATH, None)]
    # an ask without `only`: called as before, three arguments
    asked.clear()

    async def plain(path, refusal, current):
        asked.append((path, None))
        return f"```python\n{FILE}```"

    out, left = await redraw_faked_tests(OUT, [F_USE], ask=plain, check=lambda c: _aret([]), tests_only=True)
    assert asked == [(PATH, None)]


async def _aret(v):
    return v


@pytest.mark.asyncio
async def test_an_identical_answer_ends_the_loop_and_a_pass_is_counted_by_its_progress():
    calls = {"ask": 0, "check": 0}

    async def same(path, refusal, current, only=None):
        calls["ask"] += 1
        return f"```python\n{FILE}```"                      # the file as it is: nothing changed

    out, left = await redraw_faked_tests(OUT, [F_USE], ask=same, check=lambda c: _aret([F_USE]), tests_only=True)
    assert (out, left) == (OUT, [F_USE]) and calls == {"ask": 1, "check": 0}
    # a syntax pass is not counted: syntax -> 3 -> 2 -> 1 -> 0 takes four asks within tries=3
    seq = iter([[F_USE + " a", F_USE + " b", F_USE + " c"], [F_USE + " a", F_USE + " b"], [F_USE + " a"], []])
    n = {"i": 0}

    async def changing(path, refusal, current, only=None):
        n["i"] += 1
        if only:                                           # the named definition, changed each time
            return f"```python\ndef _fake_client(payload):\n    return {n['i']}\n```"
        return f"```python\n{FILE}# pass {n['i']}\n```"

    async def check(candidate):
        return next(seq)

    syntax = ["`tests/test_paper_twelve_modules.py:9` does not compile: invalid syntax -- fix that line"]
    out, left = await redraw_faked_tests(OUT, syntax, ask=changing, check=check, tries=3, tests_only=True)
    assert left == [] and n["i"] == 4 and "    return 4\n" in out
    # no progress (as many findings as before, the file changed): the loop stops with the latest
    n["i"] = 0
    out, left = await redraw_faked_tests(OUT, [F_USE], ask=changing, check=lambda c: _aret([F_USE + " z"]),
                                         tries=4, tests_only=True)
    assert left == [F_USE + " z"] and n["i"] == 1 and "    return 1\n" in out
    # progress every pass: up to `tries` passes
    n["i"] = 0
    seq = iter([[F_USE] * 3, [F_USE] * 2, [F_USE], []])
    out, left = await redraw_faked_tests(OUT, [F_USE] * 4, ask=changing, check=check, tries=4, tests_only=True)
    assert left == [] and n["i"] == 4


def test_the_executors_ask_takes_the_definitions_to_write_again():
    src = inspect.getsource(ea)
    ask = src.split("async def _ask_tests(_path: str, _refusal: str, _current: str,", 1)[1].split("async def _check_tests", 1)[0]
    assert "only: list[str] | None = None" in ask
    assert "Write again ONLY these definitions of `{_path}`, each complete with its decorators" in ask
    assert "Everything else in the file stays exactly as it is -- do not repeat it" in ask
    assert "if only else" in ask and "Keep every test." in ask                       # the whole-file ask stays
