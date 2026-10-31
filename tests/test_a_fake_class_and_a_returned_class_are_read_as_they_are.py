"""§17.1870 — the mock model is the measured one; a class the test wrote is a fake like any other (its methods'
signatures against the module's call, the dunders the module needs); a test under `pytest.raises` may never reach
its fake; a dict key whose sub-key must be given must be given; a test reads what a returned class has; a five-part
patch target names the module's first attribute; a finished module's own missing name is its defect, not the test's.

Live (5b4880c0 T5, 2026-10-30 02:27–02:43 UTC): attempts 1 and 2 were refused for "`__aenter__()` returns a fake
whose `get()` returns a MagicMock, awaited" -- measured, entering a mock gives an AsyncMock and `get()` on it is
awaitable; the real failure was `json()` on that AsyncMock, a coroutine the module reads as a dict. Attempt 4 was
refused for a fake a `pytest.raises(ValueError)` test never reaches. Attempt 3's sandbox: `class FakeClient: async def
get(self, url)` called with `params=` (8 tests), `payload={"proposal_id": 1}` with no `proposal` (4), `result.passed`
on a `RiskDecision` (6), `patch("…paper_news_ingestion.httpx.AsyncClient")` on a module without `httpx`, and the
finished FINRA ingester's own `paper_audit_trail_writer.write` -- a name that does not exist."""
from __future__ import annotations

from app.modules import engine_source as es
from app.modules.engine_contracts import (_patch_targets, unknown_patch_targets, unknown_result_attributes,
                                          wrong_fakes)
from app.sandbox import engine_tests as et

CONGRESS = '''# app/modules/paper_congressional_trade_ingestion.py
import httpx

async def _fetch_json(client, url, *, params=None):
    response = await client.get(url, params=params, timeout=30.0)
    response.raise_for_status()
    return response.json()

async def fetch_disclosures(client=None, *, max_requests: int = 3) -> list:
    data = await _fetch_json(client, "u", params={"page": 1})
    return data.get("results") or []
'''
REDDIT = '''# app/modules/paper_reddit_ingestion.py
import httpx

async def ingest_reddit_posts(subreddit: str) -> int:
    async with httpx.AsyncClient() as client:
        response = await client.get("u")
        data = response.json()
        return len(data.get("data", {}).get("children", []))
'''
ALPACA = '''# app/modules/paper_alpaca_paper_client.py
import httpx

class AlpacaPaperClient:
    async def submit_order(self, symbol: str, side: str):
        if side not in ("buy", "sell"):
            raise ValueError(side)
        client = httpx.AsyncClient()
        response = await client.request("POST", "/v2/orders")
        return response.json()
'''
RISK = '''# app/modules/paper_risk_control_module.py
from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None

def check_daily_loss(*, equity: Decimal, day_start_equity: Decimal) -> RiskDecision:
    return RiskDecision(allowed=equity >= day_start_equity * Decimal("0.97"), reason=None)
'''
NEWS = '''# app/modules/paper_news_ingestion.py
from app.modules import searxng_client

async def fetch_news_items(query: str) -> list:
    return []
'''
MODULES = {m.split("\n", 1)[0][len("# app/modules/"):-3]: m.split("\n", 1)[1] for m in (CONGRESS, REDDIT, ALPACA, RISK, NEWS)}
PATH = "tests/test_paper_twelve_modules.py"


def _files(test: str) -> dict[str, str]:
    return {PATH: "# " + PATH + "\n" + test}


def test_the_mock_model_is_the_measured_one():
    """`await AsyncMock()()` -> AsyncMock; entering either kind -> AsyncMock; an AsyncMock method called without
    await -> a coroutine; a MagicMock call awaited -> fails. The reddit shape of attempts 1 and 2, read right."""
    test = '''from unittest.mock import AsyncMock, MagicMock, patch
from app.modules import paper_reddit_ingestion

async def test_async_mock_response():
    with patch("app.modules.paper_reddit_ingestion.httpx.AsyncClient") as cls:
        mock_http = AsyncMock()
        cls.return_value = mock_http
        await paper_reddit_ingestion.ingest_reddit_posts("stocks")

async def test_magic_response():
    with patch("app.modules.paper_reddit_ingestion.httpx.AsyncClient") as cls:
        mock_http = AsyncMock()
        cls.return_value = mock_http
        mock_http.__aenter__.return_value = mock_http
        mock_http.get.return_value = MagicMock(json=MagicMock(return_value={"data": {"children": []}}))
        await paper_reddit_ingestion.ingest_reddit_posts("stocks")
'''
    found = wrong_fakes(_files(test), MODULES, set())
    assert found == [
        "`tests/test_paper_twelve_modules.py:6` patches `AsyncClient` on `httpx` with a fake whose call returns a fake whose "
        "`__aenter__()` returns a fake whose `get()` returns a fake whose `json()` returns a coroutine (line 7): an "
        "`AsyncMock` method called without `await` (every attribute of an `AsyncMock` is one), and the module reads it as "
        "a dict -- make that object a `MagicMock` (its plain calls answer at once; give the methods the module awaits an "
        "`AsyncMock` each) -- give it what the module uses: `AsyncClient()->{async with -> {get()->{json(): dict{data{children}}}}}`",
    ], found


def test_a_class_the_test_wrote_is_checked_method_by_method():
    test = '''from app.modules import paper_congressional_trade_ingestion, paper_reddit_ingestion

class FakeResponse:
    def __init__(self, json_data):
        self._json = json_data
    def raise_for_status(self):
        pass
    def json(self):
        return self._json

async def test_narrow_get():
    class FakeClient:
        async def get(self, url):
            return FakeResponse(json_data={"results": []})
    client = FakeClient()
    await paper_congressional_trade_ingestion.fetch_disclosures(client=client, max_requests=1)

async def test_fits():
    class FakeClient:
        async def get(self, url, **kwargs):
            return FakeResponse(json_data={"results": []})
    await paper_congressional_trade_ingestion.fetch_disclosures(client=FakeClient(), max_requests=1)

async def test_no_aenter(monkeypatch):
    class FakeClient:
        async def get(self, url, **kwargs):
            return FakeResponse(json_data={"data": {"children": []}})
    monkeypatch.setattr("app.modules.paper_reddit_ingestion.httpx.AsyncClient", lambda *a, **k: FakeClient())
    await paper_reddit_ingestion.ingest_reddit_posts("stocks")

async def test_sync_get():
    class FakeClient:
        def get(self, url, **kwargs):
            return FakeResponse(json_data={"results": []})
    await paper_congressional_trade_ingestion.fetch_disclosures(client=FakeClient(), max_requests=1)
'''
    found = wrong_fakes(_files(test), MODULES, set())
    assert found == [
        "`tests/test_paper_twelve_modules.py:17` hands `fetch_disclosures(…)` `client` = a fake whose `get` is `get(…)` "
        "(line 14), which has no parameter `params`, `timeout` (it takes url); the module passes `params=`, `timeout=` "
        "-- accept what the module passes (`*args, **kwargs` is enough) -- give it what the module uses: "
        "`client{get()->{json(): dict{results}, raise_for_status()}}`",
        "`tests/test_paper_twelve_modules.py:29` patches `AsyncClient` on `httpx` with a fake whose call returns a "
        "`FakeClient` instance (line 26), which has no `__aenter__`, and the module enters it with `async with` -- it "
        "needs `async def __aenter__(self): return self` and `async def __aexit__(self, *exc): pass` -- give it what the "
        "module uses: `AsyncClient()->{async with -> {get()->{json(): dict{data{children}}}}}`",
    ], found
    # the sync `get` awaited is not followed (a plain def's result awaited: unknown); `test_fits` passes


def test_a_test_that_expects_a_raise_may_never_reach_its_fake():
    test = '''import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def test_invalid_side_raises():
    fake_client = MagicMock()
    with patch("app.modules.paper_alpaca_paper_client.httpx.AsyncClient", return_value=fake_client):
        client = AlpacaPaperClient()
        with pytest.raises(ValueError):
            await client.submit_order(symbol="SPY", side="hold")

async def test_submit_for_real():
    fake_client = MagicMock()
    with patch("app.modules.paper_alpaca_paper_client.httpx.AsyncClient", return_value=fake_client):
        await AlpacaPaperClient().submit_order(symbol="SPY", side="buy")
'''
    found = wrong_fakes(_files(test), MODULES, set())
    assert len(found) == 1 and found[0].startswith("`tests/test_paper_twelve_modules.py:15` patches `AsyncClient`"), found
    assert "`request()` returns a `MagicMock` (line 14), which the module awaits" in found[0]


def test_a_key_whose_sub_key_must_be_given_must_be_given():
    gate = '''# app/modules/paper_operator_approval_gate.py
from typing import Any
from sqlalchemy import text
from app.database import async_session

async def _record_rejection(db, proposal: dict[str, Any]) -> None:
    await db.execute(text("INSERT INTO approval (proposal_id, decision) VALUES (:proposal_id, :decision)"),
                     {"proposal_id": proposal.get("proposal_id"), "decision": "rejected"})

async def handle_approval_answer(job_id: str, node_key: str, answer: dict[str, Any], payload: dict[str, Any]) -> None:
    proposal = payload.get("proposal", {})
    async with async_session() as db:
        await _record_rejection(db, proposal)
'''
    test = '''# tests/test_paper_twelve_modules.py
from app.modules import paper_operator_approval_gate

async def test_refuse():
    await paper_operator_approval_gate.handle_approval_answer("j", "n", {"decision": "rejected"}, {"proposal_id": 1})
'''
    defs = es.sibling_definitions([{"node_key": "T1", "status": "done", "output_text": "```python\n" + gate + "```"}])
    found = es.sibling_refs("```python\n" + test + "```", defs, fks={"approval": {"proposal_id": "proposal.id"}}, rules={})
    assert len(found) == 1 and found[0].startswith(
        "`tests/test_paper_twelve_modules.py:5` calls `handle_approval_answer(…)`: `payload` is read for `proposal` (its "
        "`proposal_id` is written to `approval.proposal_id`, which references `proposal`) -- the dict given has "
        "`proposal_id` and no `proposal`; pass the id its parent's writer returned"), found


def test_a_test_reads_what_a_returned_class_has():
    test = '''from decimal import Decimal
from app.modules.paper_risk_control_module import check_daily_loss

def test_halts():
    result = check_daily_loss(equity=Decimal("96000"), day_start_equity=Decimal("100000"))
    assert result.passed is False
    assert result.reason is None

def test_allows():
    decision = check_daily_loss(equity=Decimal("99000"), day_start_equity=Decimal("100000"))
    assert decision.allowed is True
'''
    assert unknown_result_attributes(_files(test), MODULES) == [
        "`tests/test_paper_twelve_modules.py:7` reads `.passed` from what `check_daily_loss` (`paper_risk_control_module`) "
        "returns, a `RiskDecision`, which has no `passed` -- it has `allowed`, `reason`"]


def test_a_five_part_patch_target_needs_the_modules_first_attribute():
    test = '''from unittest.mock import patch
from app.modules import paper_news_ingestion

async def test_news():
    with patch("app.modules.paper_news_ingestion.httpx.AsyncClient"):
        await paper_news_ingestion.fetch_news_items("AAPL")
'''
    assert _patch_targets(test) == [("paper_news_ingestion", "httpx", 5)]
    found = unknown_patch_targets(_files(test), MODULES, set())
    assert len(found) == 1 and found[0].startswith(
        "`tests/test_paper_twelve_modules.py:6` patches `httpx` on `paper_news_ingestion`, which has no such name")


def test_a_finished_modules_missing_name_is_its_own_defect():
    report = ('____________________________ test_ingest_finra_file ____________________________\n'
              'tests/test_paper_twelve_modules.py:496: in test_ingest_finra_file\n'
              '    result = await ingest_finra_file(\n'
              'app/modules/paper_finra_data_ingestion.py:225: in _record_version\n'
              '    await paper_audit_trail_writer.write(\n'
              "E   AttributeError: module 'app.modules.paper_audit_trail_writer' has no attribute 'write'\n"
              '=========================== short test summary info ============================\n'
              'FAILED tests/test_paper_twelve_modules.py::test_ingest_finra_file - AttributeError\n')
    cause = et.known_causes(report)
    assert "a finished module reaches for a name that does not exist" in cause and "the job gets a fix step for it" in cause
    assert et.callee_defects(report) == [{
        "module": "paper_finra_data_ingestion", "frame": "app/modules/paper_finra_data_ingestion.py:225 in _record_version",
        "test": "test_ingest_finra_file", "cause": et.KNOWN_CAUSES[5][1]}]
    # a missing attribute of an OBJECT raised inside a module is still the test's fake, never the module's defect
    fake = report.replace("module 'app.modules.paper_audit_trail_writer' has no attribute 'write'",
                          "'str' object has no attribute 'raise_for_status'")
    assert et.callee_defects(fake) == []
