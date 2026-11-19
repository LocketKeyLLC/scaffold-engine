"""§17.1946 — a test that reads an attribute its own fake class never has is refused before the sandbox. Live
(5b4880c0 API_ALPACA_AUDITF1, 2026-11-05 03:39 UTC): `fake_client._responses.append(…)` on a `FakeAsyncClient`
keeping no `_responses` -- an AttributeError the sandbox reported, a round each."""
from __future__ import annotations

from app.modules.engine_contracts import fake_attrs_missing

_TEST = '''import pytest


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self):
        self.requests = []

    async def get(self, url, **kw):
        self.requests.append(url)
        return FakeResponse()


@pytest.fixture
def fake_client():
    client = FakeAsyncClient()
    return client


def test_quote(fake_client):
    fake_client._responses.append(FakeResponse(status_code=200, json_data={"q": 1}))
    assert fake_client.requests == []


def test_direct():
    c = FakeAsyncClient()
    c.extra = 1
    assert c.extra == 1 and c.requests == [] and c.get
'''


def test_the_live_shape_is_named_once():
    got = fake_attrs_missing({"tests/test_paper_alpaca_paper_client.py": _TEST})
    assert [g.split(" -- ")[0] for g in got] == [
        "`tests/test_paper_alpaca_paper_client.py:29` reads `fake_client._responses`, and `fake_client` is a "
        "`FakeAsyncClient` (its own class, line 13), which has no `_responses`"]


def test_an_attribute_it_has_one_the_test_set_and_open_classes_are_not():
    fixed = _TEST.replace("        self.requests = []\n", "        self.requests = []\n        self._responses = []\n")
    assert fake_attrs_missing({"tests/test_x.py": fixed}) == []
    open_ = _TEST.replace("class FakeAsyncClient:", "class FakeAsyncClient(MagicMock):")
    assert fake_attrs_missing({"tests/test_x.py": open_}) == []
    anyname = _TEST.replace("    async def get(self, url, **kw):",
                            "    def __getattr__(self, n):\n        return None\n\n    async def get(self, url, **kw):")
    assert fake_attrs_missing({"tests/test_x.py": anyname}) == []
