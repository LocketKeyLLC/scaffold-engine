"""§17.1913 — a value the module tests for None before it acts on it takes None from a test's stand-in.

Live (follow-up 5b4880c0 T9 a1, 2026-11-04 22:49 UTC): `scheduler = get_scheduler()` then `if scheduler is not
None: scheduler.add_job(…)` -- exactly what the install note asks for ("it may return None: then nothing is
scheduled") -- and a test patching `get_scheduler` to return None was refused: "a fake whose call returns None …
and the module calls `.add_job()` on it"."""
from __future__ import annotations

from app.modules.engine_contracts import wrong_fakes

MOD = '''# app/modules/lab_wire.py
from app.scheduler import get_scheduler


def install(app) -> None:
    scheduler = get_scheduler()
    if scheduler is not None:
        scheduler.add_job(print, "cron", hour=9)


def install_unguarded(app) -> None:
    scheduler = get_scheduler()
    scheduler.add_job(print, "cron", hour=9)
'''


def _test(fn: str) -> str:
    return f'''# tests/test_lab_wire.py
from unittest.mock import patch
from app.modules.lab_wire import {fn}


def test_no_scheduler():
    with patch("app.modules.lab_wire.get_scheduler", return_value=None):
        {fn}(object())
'''


def test_none_is_taken_where_the_module_tests_for_it_and_refused_where_it_does_not():
    body = MOD.split("\n", 1)[1]
    guarded, unguarded = body.split("\n\ndef install_unguarded")
    mods = {"lab_wire": guarded}
    assert wrong_fakes({"tests/test_lab_wire.py": _test("install")}, mods, {"lab_wire"}) == []
    # every place the module binds it must test it: one that does not is enough for None to break it
    mods = {"lab_wire": guarded + "\n\ndef install_unguarded" + unguarded}
    got = wrong_fakes({"tests/test_lab_wire.py": _test("install")}, mods, {"lab_wire"})
    assert len(got) == 1 and "returns None" in got[0] and "`.add_job()`" in got[0], got


def test_a_fix_steps_key_is_cited_as_itself_on_a_verdict_line():
    """§17.1913 — live (API_REDDIT_AUDIT a1-a3): "MISSING: ['T6U7']" with every verdict line citing `(T6U7)`."""
    from app.modules.execution_verify import check_validation_citation_coverage
    out = ("## Verdicts\n"
           "- **NOT MET — `paper_reddit_ingestion` (T6U7):** sends no `Authorization: bearer TOKEN` header\n"
           "- MET — `paper_spy_benchmark_tracker` (T5U4): reads bars from data.alpaca.markets\n"
           "- the proposer (T1F2) is mentioned outside any verdict\n")
    assert check_validation_citation_coverage(out, ["T6U7", "T5U4"]) == []
    assert check_validation_citation_coverage(out, ["T6U7", "T1F2", "T7"]) == ["T1F2", "T7"]
    assert check_validation_citation_coverage("- MET — T2 lines 5-15", ["T2", "T3"]) == ["T3"]   # numeric keys as before



def test_a_bare_mock_compared_with_a_number_is_named_and_a_real_response_is_offered():
    """§17.1915 — live (API_ALPACA_AUDITF1 a2): "'>=' not supported between instances of 'AsyncMock' and 'int'" x8."""
    import httpx

    from app.sandbox import engine_tests as et
    rx, hint = et.KNOWN_CAUSES[12]                                        # stable index
    for line in ("E   TypeError: '>=' not supported between instances of 'AsyncMock' and 'int'",
                 "E   TypeError: '<' not supported between instances of 'MagicMock' and 'float'"):
        assert rx.search(line), line
    assert not rx.search("E   TypeError: '>=' not supported between instances of 'str' and 'int'")
    assert "httpx.Response(200, json={...}" in hint
    r = httpx.Response(200, json={"id": "a"}, request=httpx.Request("GET", "https://paper-api.alpaca.markets/v2/account"))
    assert r.status_code == 200 and r.json() == {"id": "a"}              # the stand-in the hint names works as written


def test_a_builtin_value_is_held_to_what_it_really_has():
    """§17.1917 — live (API_ALPACA_AUDITF3 a2): a set refused for the module's `.add()`."""
    mod = ("def _flush_buffered(seen, items):\n"
           "    for i in items:\n"
           "        seen.add(i)\n"
           "    return seen\n")
    test = ('# tests/test_lab_fill.py\nfrom app.modules.lab_fill import _flush_buffered\n\n\n'
            'def test_flush():\n    seen = set()\n    _flush_buffered(seen, [1])\n')
    assert wrong_fakes({"tests/test_lab_fill.py": test}, {"lab_fill": mod}, {"lab_fill"}) == []
    bad = test.replace("seen = set()", "seen = 3")                    # an int has no `.add`: still refused
    got = wrong_fakes({"tests/test_lab_fill.py": bad}, {"lab_fill": mod}, {"lab_fill"})
    assert len(got) == 1 and "`.add()`" in got[0], got


def test_a_name_nothing_binds_is_refused_before_the_sandbox_and_a_bound_one_is_not():
    """§17.1918 — live (API_REDDIT_AUDITF1 a1): `text(insert_sql)` with no `from sqlalchemy import text`."""
    from app.modules.engine_contracts import undefined_names
    mod = '''# app/modules/lab_reddit.py
import json
from typing import Any

LIMIT = 5


class Store:
    kind = "x"

    def put(self, row: dict) -> str:
        return self.kind + json.dumps(row)


def _helper(x):
    return [y for y in x if y] + [len(x), LIMIT]


async def ingest(db, rows: list[Any]) -> int:
    global _COUNT
    _COUNT = len(rows)
    sql = "INSERT INTO t VALUES (:v)"
    await db.execute(text(sql), {"v": json.dumps(rows)})
    try:
        pass
    except ValueError as exc:
        print(exc, Store, _helper, __name__)
    return missing_thing + 1
'''
    got = undefined_names({"app/modules/lab_reddit.py": mod})
    assert [g.split(" reads ")[1].split(",")[0] for g in got] == ["`text`", "`missing_thing`"], got
    assert got[0].startswith("`app/modules/lab_reddit.py:23` reads `text`") and "`from sqlalchemy import text`" in got[0]
    assert undefined_names({"app/modules/lab_reddit.py": mod.replace("import json\n", "import json\nfrom sqlalchemy import text\nmissing_thing = 1\n")}) == []
    assert undefined_names({"app/x.py": "from os.path import *\nprint(join)\n"}) == []         # a star import: left alone


def test_a_patch_given_a_return_value_inside_a_yield_fixture_is_held_to_that_value():
    """§17.1921 — live (API_ALPACA_AUDITF1 a3): `patch("…httpx.AsyncClient", return_value=fake_client)` inside fixture
    `patched`, which also nests `class DummySession: async def __aenter__(self): return None` -- the gate read that
    nested `return None` as the patch's value: "a fake whose call returns None (line 55)"."""
    mod = ("import httpx\n\n\nasync def fetch():\n    client = httpx.AsyncClient()\n"
           "    r = await client.request('GET', 'x')\n    await client.aclose()\n    return r\n")
    test = '''# tests/test_lab_c.py
from unittest.mock import patch
import pytest
from app.modules.lab_c import fetch


class FakeClient:
    async def request(self, method, url):
        return 1

    async def aclose(self):
        pass


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def patched(fake_client):
    class Dummy:
        async def __aenter__(self):
            return None

    with patch("app.modules.lab_c.httpx.AsyncClient", return_value=fake_client):
        yield fake_client


async def test_fetch(patched):
    assert await fetch() == 1
'''
    assert wrong_fakes({"tests/test_lab_c.py": test}, {"lab_c": mod}, {"lab_c"}) == []
    bad = test.replace("return_value=fake_client", "return_value=None")
    got = wrong_fakes({"tests/test_lab_c.py": bad}, {"lab_c": mod}, {"lab_c"})
    assert len(got) == 1 and "returns None" in got[0], got                   # a real None is still refused
