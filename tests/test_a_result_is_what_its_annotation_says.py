"""§17.1876 — a result is what its annotation says; a fake dict carries a key the module reads; every name a test uses
is bound; a settings field that exists; a library patched by the name the module leaves through.

Live (follow-up 5b4880c0 T5, 2026-10-30, the §17.1875 round): three of four attempts reached the sandbox with zero
findings and failed 32, 14 and 25 tests there. Of a4's 25: nine asserted `isinstance(result, dict)` on
`handle_approval_answer(…)`, which is `-> None`; three compared a returned list or dict with a number; three handed
`fetch_disclosures` a client whose `json()` returned `{"disclosures": […]}` where the module reads `results` / `data`
/ `filings`; three patched `app.config.settings.REDDIT_MAX_POSTS` (the field is `reddit_max_posts`); two called
`ingest_13f_holdings`, whose import a redraw had dropped -- and the sandbox blamed "a finished module". a2 patched
`httpx` whole on the reddit ingester, whose `except (httpx.HTTPError, …)` then raised TypeError, three times."""
from __future__ import annotations

from app.modules import engine_source as es
from app.modules.engine_contracts import undefined_test_names, unknown_patch_targets, wrong_fakes, wrong_result_uses
from app.sandbox.engine_tests import KNOWN_CAUSES

GATE = '''# app/modules/paper_operator_approval_gate.py
async def handle_approval_answer(job_id: str, node_key: str, answer: dict, payload: dict) -> None:
    if not answer.get("approved"):
        return
    return
'''

NEWS = '''# app/modules/paper_news_ingestion.py
async def ingest_news_for_ticker(ticker: str, db=None) -> list[dict]:
    return []

async def count_items(db) -> int:
    return 0

def ingest_form_4_for_tickers(db, ciks: dict) -> dict[str, int]:
    return {}

def total(db) -> "int | None":
    return None
'''

CONGRESS = '''# app/modules/paper_congressional_trade_ingestion.py
import httpx
from dataclasses import dataclass, field

@dataclass
class FetchResult:
    rows: list = field(default_factory=list)

async def _fetch_json(client, url):
    response = await client.get(url)
    response.raise_for_status()
    return response.json()

async def fetch_disclosures(client, *, max_requests: int = 50) -> FetchResult:
    result = FetchResult()
    data = await _fetch_json(client, "https://x")
    records = data.get("results") or data.get("data") or data["filings"]
    result.rows.extend(records)
    return result
'''

REDDIT = '''# app/modules/paper_reddit_ingestion.py
import httpx
from app.config import settings

async def _fetch_reddit_listing(url: str) -> list:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            return response.json().get("data", [])
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        return []

async def ingest_reddit_posts(subreddit: str, max_posts: int | None = None) -> int:
    max_posts = max_posts if max_posts is not None else settings.reddit_max_posts
    return len(await _fetch_reddit_listing("https://r/" + subreddit))
'''

MODULES = {"paper_operator_approval_gate": GATE, "paper_news_ingestion": NEWS,
           "paper_congressional_trade_ingestion": CONGRESS, "paper_reddit_ingestion": REDDIT}
PATH = "tests/test_paper_twelve_modules.py"


def _results(test: str) -> list[str]:
    return wrong_result_uses({PATH: test}, MODULES)


def test_a_result_of_a_function_that_returns_nothing_is_not_asserted_on():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_operator_approval_gate import handle_approval_answer

async def test_it():
    result = await handle_approval_answer(job_id="j", node_key="n", answer={"approved": True}, payload={})
    assert isinstance(result, dict)
'''
    assert _results(test) == [
        "`tests/test_paper_twelve_modules.py:6` uses `result` (`isinstance(result, dict)`), what "
        "`handle_approval_answer(…)` (`paper_operator_approval_gate`) returns -- and `handle_approval_answer` returns "
        "nothing (`-> None`) -- assert on what it did (the rows it wrote, the mock it called), never on its result"]
    # `assert result is None` is the one thing a test may say about it
    assert _results(test.replace("assert isinstance(result, dict)", "assert result is None")) == []


def test_a_returned_list_or_dict_is_not_compared_with_a_number():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_news_ingestion import ingest_news_for_ticker, ingest_form_4_for_tickers

async def test_news():
    result = await ingest_news_for_ticker("SPY")
    assert result >= 0

def test_edgar():
    out = ingest_form_4_for_tickers(None, {"AAPL": 1})
    assert 0 <= out
    assert isinstance(out, list)
'''
    assert _results(test) == [
        "`tests/test_paper_twelve_modules.py:6` compares `result` (`result >= 0`), what `ingest_news_for_ticker(…)` "
        "(`paper_news_ingestion`) returns -- and `ingest_news_for_ticker` returns a `list` -- compare `len(result)`, "
        "or read its items",
        "`tests/test_paper_twelve_modules.py:10` compares `out` (`0 <= out`), what `ingest_form_4_for_tickers(…)` "
        "(`paper_news_ingestion`) returns -- and `ingest_form_4_for_tickers` returns a `dict` -- compare `len(out)`, "
        "or read its items",
        "`tests/test_paper_twelve_modules.py:11` asserts `isinstance(out, list)` on `out`, what "
        "`ingest_form_4_for_tickers(…)` (`paper_news_ingestion`) returns -- and `ingest_form_4_for_tickers` returns a "
        "`dict`, never a `list`"]
    fine = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_news_ingestion import ingest_news_for_ticker, count_items, total

async def test_news():
    result = await ingest_news_for_ticker("SPY")
    assert len(result) >= 0
    assert isinstance(result, list)
    assert result[0]["title"]
    n = await count_items(None)
    assert n >= 0
    assert isinstance(n, int)
    t = total(None)
    assert t is None or t >= 0
    result = 5
    assert result >= 0
'''
    assert _results(fine) == []


def test_a_returned_int_is_not_subscripted_or_measured():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_news_ingestion import count_items

async def test_it():
    n = await count_items(None)
    assert n["count"] == 0
    assert len(n) == 0
'''
    got = _results(test)
    assert [g.split(", what")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:6` subscripts `n` (`n['count']`)",
        "`tests/test_paper_twelve_modules.py:7` measures `n` (`len(n)`)"], got
    assert got[0].endswith("returns an `int`, which has no items")
    # a class result is the §17.1870 check's; an unannotated function says nothing
    cls = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_congressional_trade_ingestion import fetch_disclosures

async def test_it():
    result = await fetch_disclosures(None)
    assert result >= 1
'''
    assert _results(cls) == []


def test_a_fake_dict_with_none_of_the_keys_the_module_reads_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_congressional_trade_ingestion import fetch_disclosures

async def test_it():
    fake_response = MagicMock()
    fake_response.json.return_value = {"disclosures": [{"asset_ticker": "AAPL"}]}
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    result = await fetch_disclosures(fake_client, max_requests=3)
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert got == [
        "`tests/test_paper_twelve_modules.py:10` hands `fetch_disclosures(…)` `client` = a fake whose `get()` returns a "
        "fake whose `json()` returns a dict with keys `disclosures` (line 7), which the module reads for `data` / "
        "`filings` / `results` -- none of them is there -- give it what the module uses: "
        "`client{get()->{json(): dict{data|filings|results}, raise_for_status()}}`"], got
    # one of the keys there: fine; an empty dict: the "no data" case, fine
    assert wrong_fakes({PATH: test.replace('{"disclosures": [', '{"results": [')}, MODULES, set()) == []
    assert wrong_fakes({PATH: test.replace('{"disclosures": [{"asset_ticker": "AAPL"}]}', "{}")}, MODULES, set()) == []


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


def test_the_index_shows_the_keys_a_module_reads_from_a_dict_and_what_a_function_returns():
    defs = es.sibling_definitions([_row("T2", CONGRESS), _row("T3", GATE), _row("T4", NEWS)])
    me = {"node_key": "T5", "title": "Write tests", "description": "", "prompt_template": "Write tests.", "output_text": None}
    b = es.block_for(me, set(), modules=defs)
    sec = b[b.index("This job's modules"):b.index("### Conventions")]
    assert "fetch_disclosures(client{get()->{json(): dict{data|filings|results}, raise_for_status()}}, *, " \
           "max_requests: int = …) -> FetchResult" in sec, sec
    assert "handle_approval_answer(job_id: str, node_key: str, answer: dict{approved}, payload: dict) -> None" in sec, sec
    assert "ingest_news_for_ticker(ticker: str, db=…) -> list" in sec, sec
    assert "total(db) -> int | None" in sec, sec
    assert "`-> None` means it returns nothing" in b
    api = es._public_api(GATE + NEWS)
    assert "handle_approval_answer(job_id: str, node_key: str, answer: dict, payload: dict) -> None" in api, api
    assert "ingest_news_for_ticker(ticker: str, db) -> list" in api, api
    assert "ingest_form_4_for_tickers(db, ciks: dict) -> dict" in api, api
    assert "total(db) -> int | None" in api, api
    # a function whose dicts' keys are read keeps the §17.1837 note
    keyed = '''# app/modules/m.py
def load(db) -> dict:
    return {"equity": 1, "orders": 2}
'''
    assert "load(db) -> {equity, orders}" in es._public_api(keyed), es._public_api(keyed)


def test_a_name_a_test_uses_and_never_binds_is_refused_with_its_import():
    test = '''# tests/test_paper_twelve_modules.py
import pytest
from app.modules.paper_news_ingestion import ingest_news_for_ticker

class TestThirteenF:
    @pytest.mark.asyncio
    async def test_ingest_13f_holdings(self, db_session):
        result = await ingest_13f_holdings("0001067983", db=db_session)
        assert result >= 0
'''
    mods = {**MODULES, "paper_13f_holdings_ingestion": "# app/modules/paper_13f_holdings_ingestion.py\n"
                                                      "async def ingest_13f_holdings(cik, db=None) -> int:\n    return 0\n"}
    assert undefined_test_names({PATH: test}, mods) == [
        "`tests/test_paper_twelve_modules.py:8` uses `ingest_13f_holdings`, which the file never imports or defines -- "
        "`NameError` the instant the test runs; `from app.modules.paper_13f_holdings_ingestion import ingest_13f_holdings`"]
    bound = '''# tests/test_paper_twelve_modules.py
import pytest
from unittest.mock import patch
from app.modules.paper_news_ingestion import ingest_news_for_ticker

ROWS = [{"n": i} for i in range(3)]

def _helper(x):
    def inner():
        return x + len(ROWS)
    return inner

class TestIt:
    attr = 1

    @pytest.mark.asyncio
    async def test_it(self, db_session):
        items = [r["n"] for r in ROWS if r]
        with patch("x.y") as m, open(__file__) as fh:
            pass
        try:
            pass
        except ValueError as exc:
            print(exc, m, fh, items, self.attr, __name__)
        if (n := len(items)) > 0:
            assert n and _helper(1)() and isinstance(ingest_news_for_ticker, object)
'''
    assert undefined_test_names({PATH: bound}, MODULES) == []
    star = "# tests/test_x.py\nfrom tests._shared import *\n\ndef test_it():\n    assert make_mock_db()\n"
    assert undefined_test_names({"tests/test_x.py": star}, MODULES) == []


def test_a_settings_field_the_engine_lacks_is_refused_by_its_real_name():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import patch
from app.config import settings
from app.modules.paper_reddit_ingestion import ingest_reddit_posts

async def test_it():
    with patch("app.config.settings.REDDIT_MAX_POSTS", 10), patch.object(settings, "no_such_field_xyz", 1):
        await ingest_reddit_posts("stocks")
'''
    got = unknown_patch_targets({PATH: test}, MODULES, set())
    assert got == [
        "`tests/test_paper_twelve_modules.py:7` patches `REDDIT_MAX_POSTS` on `app.config.settings`, which has no such "
        "field -- `patch` raises AttributeError before the test body runs; the field is `reddit_max_posts`",
        "`tests/test_paper_twelve_modules.py:7` patches `no_such_field_xyz` on `app.config.settings`, which has no such "
        "field -- `patch` raises AttributeError before the test body runs; the job's modules read `settings.reddit_max_posts`"], got
    assert unknown_patch_targets({PATH: test.replace("REDDIT_MAX_POSTS", "reddit_max_posts")
                                  .replace(', patch.object(settings, "no_such_field_xyz", 1)', "")}, MODULES, set()) == []


def test_a_library_patched_whole_on_a_module_that_catches_its_exceptions_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import patch
from app.modules.paper_reddit_ingestion import ingest_reddit_posts

async def test_it():
    with patch("app.modules.paper_reddit_ingestion.httpx") as fake_httpx:
        await ingest_reddit_posts("stocks", max_posts=1)
'''
    assert unknown_patch_targets({PATH: test}, MODULES, set()) == [
        "`tests/test_paper_twelve_modules.py:6` patches `httpx` whole on `paper_reddit_ingestion`: every class on it "
        "becomes a mock, and the module's `except (httpx.HTTPError, httpx.TimeoutException)` then fails with "
        "`TypeError: catching classes that do not inherit from BaseException is not allowed` -- patch only the name it "
        "leaves the engine through (`app.modules.paper_reddit_ingestion.httpx.AsyncClient`)"]
    assert unknown_patch_targets({PATH: test.replace(".httpx\"", ".httpx.AsyncClient\"")}, MODULES, set()) == []
    # a module that never catches the library's exceptions: a whole patch works there
    lenient = {**MODULES, "paper_reddit_ingestion": REDDIT.replace("    except (httpx.HTTPError, httpx.TimeoutException) as exc:",
                                                                   "    except Exception as exc:")}
    assert unknown_patch_targets({PATH: test}, lenient, set()) == []


def test_the_sandbox_blames_a_module_for_a_name_error_only_in_the_modules_own_frame():
    rx = KNOWN_CAUSES[5][0]
    assert "name that does not exist" in KNOWN_CAUSES[5][1]
    in_module = ("app/modules/paper_finra_data_ingestion.py:40: in ingest_finra_file\n"
                 "    rows = parse_rows(text)\n"
                 "           ^^^^^^^^^^\n"
                 "E   NameError: name 'parse_rows' is not defined\n")
    assert rx.search(in_module)
    assert rx.search(in_module.replace("           ^^^^^^^^^^\n", ""))
    in_test = ("tests/test_paper_twelve_modules.py:1090: in test_ingest_13f_holdings\n"
               "    result = await ingest_13f_holdings(\"0001067983\", db=db)\n"
               "                   ^^^^^^^^^^^^^^^^^^^\n"
               "E   NameError: name 'ingest_13f_holdings' is not defined\n")
    assert not rx.search(in_test)
    assert rx.search("E   AttributeError: module 'app.modules.paper_audit_trail_writer' has no attribute 'write'")
