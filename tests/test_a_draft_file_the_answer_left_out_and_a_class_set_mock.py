"""§17.1926 — a file the leaked reasoning draft wrote whole and the answer after `</think>` left out is the step's
(the answer's own copy of a file still wins, and a draft block that does not compile is never taken); and a fake
class's `self.request = AsyncMock()` is followed through what the class sets on it (`self.request.return_value =
FakeResponse()`), so a response whose `json` is `async def` is named as the `async def` it is, not an AsyncMock child.

Live (follow-up 5b4880c0, 2026-11-05, 07:05–08:03 UTC, three rounds each): API_SEC_EDGAR_AUDITF1 drafted the 13F
module with a new `MissingSecretError`, wrote "Now output both files.</think>", then the test file alone -- the tests
were read against the module carried from T6U8 ("has no `MissingSecretError`") and refused. API_ALPACA_AUDITF1's
`FakeResponse.json` was `async def`; the finding said "an `AsyncMock` method called without `await` ... make that
object a `MagicMock`" -- the object was a class the test wrote, and every round kept the `async def`."""
from __future__ import annotations

from app.modules.engine_contracts import replace_file_block, wrong_fakes
from app.sandbox.engine_tests import after_reasoning, files_of

MOD_PATH = "app/modules/paper_13f_holdings_ingestion.py"
TEST_PATH = "tests/test_paper_13f_holdings_ingestion.py"

DRAFT_MOD = f'''```python
# {MOD_PATH}
class MissingSecretError(RuntimeError):
    pass


async def ingest_13f_holdings(db, client):
    return 0
```'''

ANSWER_TEST = f'''```python
# {TEST_PATH}
from app.modules.paper_13f_holdings_ingestion import MissingSecretError


def test_error():
    assert issubclass(MissingSecretError, RuntimeError)
```'''


def test_a_file_the_draft_wrote_whole_and_the_answer_left_out_is_kept():
    out = DRAFT_MOD + "\n\nNow the test file. ... Now output both files.</think>" + ANSWER_TEST
    files = files_of({"output_text": out})
    assert sorted(files) == [MOD_PATH, TEST_PATH]
    assert "class MissingSecretError" in files[MOD_PATH]
    # a redraw of the tests keeps the module beside them
    again = replace_file_block(out, TEST_PATH, ANSWER_TEST.replace("test_error", "test_error_again"))
    assert sorted(files_of({"output_text": again})) == [MOD_PATH, TEST_PATH]
    assert "test_error_again" in files_of({"output_text": again})[TEST_PATH]


def test_the_answers_own_copy_wins_and_a_half_written_draft_is_not_a_file():
    answer_mod = DRAFT_MOD.replace("return 0", "return 1")
    out = DRAFT_MOD + "\n</think>" + answer_mod + "\n" + ANSWER_TEST
    assert "return 1" in files_of({"output_text": out})[MOD_PATH]
    broken = DRAFT_MOD.replace("async def ingest_13f_holdings(db, client):\n    return 0", "async def ingest(db,")
    assert sorted(files_of({"output_text": broken + "\n</think>" + ANSWER_TEST})) == [TEST_PATH]
    assert after_reasoning("no reasoning here") == "no reasoning here"


CLIENT_MOD = '''# app/modules/paper_alpaca_paper_client.py
import httpx


class AlpacaPaperClient:
    def __init__(self):
        self._client = None

    async def get_account(self) -> dict:
        response = await self._client.request("GET", "/v2/account")
        data = response.json()
        return {"cash": data.get("cash")}
'''


def _test(json_def: str) -> str:
    return f'''# tests/test_paper_alpaca_paper_client.py
import pytest
from unittest.mock import AsyncMock
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient


class FakeResponse:
    def __init__(self, json_data=None):
        self._json_data = json_data or {{}}

    {json_def}
        return self._json_data


class FakeAsyncClient:
    def __init__(self):
        self.request = AsyncMock()
        self.request.return_value = FakeResponse({{"cash": "1"}})


@pytest.mark.asyncio
async def test_account():
    client = AlpacaPaperClient()
    client._client = FakeAsyncClient()
    assert (await client.get_account())["cash"] == "1"
'''


def test_a_class_set_response_with_an_async_json_is_named_as_the_async_def_it_is():
    mods = {"app/modules/paper_alpaca_paper_client.py": CLIENT_MOD}
    out = wrong_fakes({"tests/test_paper_alpaca_paper_client.py": _test("async def json(self):")}, mods, set(mods))
    assert len(out) == 1
    assert "the fake is `async def`" in out[0] and "make the fake a plain `def`" in out[0]
    assert "AsyncMock` method called without" not in out[0]
    # and a plain `def json` is no finding at all
    assert wrong_fakes({"tests/test_paper_alpaca_paper_client.py": _test("def json(self):")}, mods, set(mods)) == []
