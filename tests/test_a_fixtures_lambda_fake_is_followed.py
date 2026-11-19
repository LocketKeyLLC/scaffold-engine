"""§17.1944 — a fake patched in by a fixture through a lambda (`monkeypatch.setattr(…, "AsyncClient", lambda *a, **k:
fake_client)`) is followed in the fixture's own scope, and a mock attribute the module compares with a number is
named. Live (5b4880c0 API_ALPACA_AUDITF1 a2, 2026-11-05 00:08 UTC): eight tests "'>=' not supported between instances
of 'AsyncMock' and 'int'" -- `if response.status_code >= 400:` on the fixture's bare `response_mock`, caught only by
the sandbox (parked since §17.1915)."""
from __future__ import annotations

from app.modules import engine_contracts as ec

_MOD = '''import httpx


async def get_account() -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get("https://paper-api.alpaca.markets/v2/account")
        if response.status_code >= 400:
            raise RuntimeError(response.text)
        return response.json()
'''
_TEST = '''from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import paper_alpaca_client


@pytest.fixture
def patched(monkeypatch):
    response_mock = AsyncMock()
    fake_client = AsyncMock()
    fake_client.get.return_value = response_mock
    fake_client.__aenter__.return_value = fake_client
    monkeypatch.setattr(paper_alpaca_client.httpx, "AsyncClient", lambda *a, **k: fake_client)
    return {"client": fake_client, "response": response_mock}


@pytest.mark.asyncio
async def test_account(patched):
    patched["response"].json = MagicMock(return_value={"cash": "100"})
    assert (await paper_alpaca_client.get_account())["cash"] == "100"
'''


def _found(test: str) -> list[str]:
    files = {"app/modules/paper_alpaca_client.py": _MOD, "tests/test_paper_alpaca_client.py": test}
    return ec.wrong_fakes(files, {"paper_alpaca_client": _MOD}, {"paper_alpaca_client"})


def test_the_fixtures_lambda_is_followed_to_the_bare_response():
    got = _found(_TEST)
    assert len(got) == 1
    assert "returns a fake whose `get()` returns a fake whose `.status_code` is a mock (line 10)" in got[0]
    assert "compares it with a number" in got[0]


def test_a_status_code_the_fixture_sets_is_not():
    fixed = _TEST.replace("    response_mock = AsyncMock()\n",
                          "    response_mock = AsyncMock()\n    response_mock.status_code = 200\n")
    assert fixed != _TEST and _found(fixed) == []


def test_a_bare_mocks_own_child_is_not():
    """A response the test never handed in (`mock_instance.get()`'s auto child) stands on a path it may not take."""
    bare = _TEST.replace("    fake_client.get.return_value = response_mock\n", "")
    assert bare != _TEST and _found(bare) == []
