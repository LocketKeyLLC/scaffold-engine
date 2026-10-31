"""§17.1878 — a hung test is named by pytest, not swallowed by the run's cap; a mock attribute the module binds to a
column is refused; a value handed on after a call keeps what its reader does with it; an exception class patched on
a module that catches it is refused.

Live (follow-up 5b4880c0 T5, 2026-10-30, the §17.1877 round, 23:06–23:27 UTC): three of four attempts reached the
sandbox with zero findings. a1 "timed out after 180s" -- three tests hung at the 60 s per-test timeout and the run's
own cap ended it before pytest named one. a2 and a3 failed six tests each on `expected str, got AsyncMock`: the
executor's `submit_order = AsyncMock()` with no return value, `order.id` an AsyncMock bound to a str column. a3 failed
two 13F tests on `'list' object has no attribute 'get'`: the fake `json()` returned a list where `_find_13f_hr_rows`
reads `.get("filings")` -- the index showed `json()` with no dict, the uses tree having copied the fetched result
before the caller's reader filled it. a3's reddit tests died in `except (httpx.HTTPError, …)` with "catching classes
that do not inherit from BaseException"."""
from __future__ import annotations

import ast

from app.modules import engine_source as es
from app.modules.engine_contracts import _result_uses, unknown_patch_targets, wrong_fakes
from app.sandbox import engine_tests as et

EXECUTOR = '''# app/modules/paper_order_executor.py
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient
from sqlalchemy import text

async def execute_order(approval: dict, db) -> dict:
    client = AlpacaPaperClient()
    try:
        order = await client.submit_order(symbol=approval["symbol"])
    finally:
        await client.close()
    broker_id = order.id
    params = {"approval_id": approval["approval_id"], "broker_order_id": broker_id}
    await db.execute(text("INSERT INTO broker_order_id (approval_id, broker_order_id) VALUES (:approval_id, :broker_order_id)"), params)
    await db.execute(text("UPDATE proposal SET note = :note"), {"note": order.status})
    return {"status": "submitted", "broker_order_id": order.id, "client_order_id": order.client_order_id}
'''

THIRTEEN_F = '''# app/modules/paper_13f_holdings_ingestion.py
import httpx

async def _rate_limited_request(client, url: str) -> httpx.Response:
    return await client.get(url)

async def _fetch_submissions(client, cik: str) -> dict:
    response = await _rate_limited_request(client, "https://x/" + cik)
    response.raise_for_status()
    return response.json()

def _find_13f_hr_rows(submissions: dict) -> list:
    recent = submissions.get("filings", {}).get("recent", {})
    return list(recent)

async def ingest_13f_holdings(cik: str, *, client) -> int:
    submissions = await _fetch_submissions(client, cik)
    rows = _find_13f_hr_rows(submissions)
    return len(rows)
'''

REDDIT = '''# app/modules/paper_reddit_ingestion.py
import httpx

async def _fetch_reddit_listing(url: str) -> list:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            return response.json().get("data", [])
    except (httpx.HTTPError, httpx.TimeoutException):
        return []

async def ingest_reddit_posts(subreddit: str) -> int:
    return len(await _fetch_reddit_listing("https://r/" + subreddit))
'''

MODULES = {"paper_order_executor": EXECUTOR, "paper_13f_holdings_ingestion": THIRTEEN_F, "paper_reddit_ingestion": REDDIT}
PATH = "tests/test_paper_twelve_modules.py"


def test_the_uses_tree_marks_what_the_module_binds_to_a_column_through_a_name_a_dict_or_inline():
    u = _result_uses(EXECUTOR, lambda c: getattr(c.func, "id", None) == "AlpacaPaperClient")
    assert u is not None
    order = u["called"]["then"]["calls"]["submit_order"]["then"]
    assert order["attrs"] == {"id", "status", "client_order_id"}
    assert order["bound"] == {"id", "status"}                  # `client_order_id` is only returned, never bound
    assert es._uses_text(u) == "()->{close(), submit_order()->{client_order_id, id, status}}"


def test_a_fake_whose_bound_attribute_is_a_mock_is_refused_and_a_set_one_passes():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_order_executor import execute_order

async def test_it(db):
    fake_client = MagicMock()
    fake_client.submit_order = AsyncMock()
    fake_client.close = AsyncMock()
    with patch("app.modules.paper_order_executor.AlpacaPaperClient", MagicMock(return_value=fake_client)):
        result = await execute_order({"symbol": "SPY", "approval_id": 1}, db)
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert got == [
        "`tests/test_paper_twelve_modules.py:9` patches `AlpacaPaperClient` on `paper_order_executor` with a fake whose "
        "call returns a fake whose `submit_order()` returns a fake whose `.id` is a mock (line 7): the module writes "
        "`.id` to a database column, and a mock is not a value -- set `fake.id = \"…\"` on the object the call "
        "returns (`AsyncMock(return_value=SimpleNamespace(id=\"…\"))`) -- give it what the module uses: "
        "`AlpacaPaperClient()->{close(), submit_order()->{client_order_id, id, status}}`"], got
    fixed = test.replace("fake_client.submit_order = AsyncMock()",
                         "fake_order = MagicMock()\n    fake_order.id = \"o-1\"\n    fake_order.status = \"accepted\"\n"
                         "    fake_order.client_order_id = \"c-1\"\n"
                         "    fake_client.submit_order = AsyncMock(return_value=fake_order)")
    assert wrong_fakes({PATH: fixed}, MODULES, set()) == []


def test_a_result_handed_on_keeps_what_its_reader_does_with_it():
    tree = ast.parse(THIRTEEN_F)
    local = es._local_functions(tree)
    assert es._uses_text(es._uses(local["ingest_13f_holdings"], "client", local)) == \
        "{get()->{json(): dict{filings{recent}}, raise_for_status()}}"
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_13f_holdings_ingestion import ingest_13f_holdings

async def test_it():
    response = MagicMock()
    response.json.return_value = [{"filings": {}}]
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    assert await ingest_13f_holdings("0001", client=client) == 0
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert got == [
        "`tests/test_paper_twelve_modules.py:10` hands `ingest_13f_holdings(…)` `client` = a fake whose `get()` returns "
        "a fake whose `json()` returns a list (line 7), which the module reads as a dict (`.get(…)` / `[\"key\"]`) -- "
        "give it what the module uses: `client{get()->{json(): dict{filings{recent}}, raise_for_status()}}`"], got
    assert wrong_fakes({PATH: test.replace('[{"filings": {}}]', '{"filings": {}}')}, MODULES, set()) == []


def test_an_exception_class_patched_on_a_module_that_catches_it_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import patch
from app.modules.paper_reddit_ingestion import ingest_reddit_posts

async def test_it():
    with patch("app.modules.paper_reddit_ingestion.httpx.HTTPError"):
        await ingest_reddit_posts("stocks")
'''
    assert unknown_patch_targets({PATH: test}, MODULES, set()) == [
        "`tests/test_paper_twelve_modules.py:6` patches `httpx.HTTPError` on `paper_reddit_ingestion`, an exception class "
        "the module catches (`except (httpx.HTTPError, httpx.TimeoutException)`): a mock is not an exception class, and "
        "the `except` fails with `TypeError: catching classes that do not inherit from BaseException is not allowed` -- "
        "patch only the name it leaves the engine through (`app.modules.paper_reddit_ingestion.httpx.AsyncClient`)"]
    assert unknown_patch_targets({PATH: test.replace("httpx.HTTPError", "httpx.AsyncClient")}, MODULES, set()) == []


def test_the_sandbox_names_a_hung_test_and_recognises_it():
    import inspect
    src = inspect.getsource(et.check)
    assert '"--timeout=20"' in src and '"--timeout=60"' not in src
    from app.config import Settings
    assert Settings.model_fields["engine_test_timeout_s"].default == 480.0
    hung = ("tests/test_paper_twelve_modules.py:858: in test_ingest_reddit_posts\n"
            "    result = await paper_reddit_ingestion.ingest_reddit_posts()\n"
            "E   Failed: Timeout >20.0s\n")
    hits = [h for rx, h in et.KNOWN_CAUSES if rx.search(hung)]
    assert any(h.startswith("a test hung until the sandbox's per-test timeout") for h in hits), hits
    assert et.KNOWN_CAUSES[6][0].search("E       Failed: Timeout >60.0s")
    assert not et.KNOWN_CAUSES[6][0].search("httpx.TimeoutException: timed out")
    # the table's earlier entries keep their places (indexed by callers)
    assert et.KNOWN_CAUSES[1][0] is et._MOCK_AWAITED and et.CALLEE_CAUSES == (et.KNOWN_CAUSES[0], et.KNOWN_CAUSES[5])
    # a run that timed out whole still carries what pytest printed
    assert "_tail(res.stdout" in src.split("if res.timed_out:", 1)[1].split("elif", 1)[0]
