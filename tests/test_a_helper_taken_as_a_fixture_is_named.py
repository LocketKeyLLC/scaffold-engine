"""§17.1943 — a test whose parameter names a plain function of its own file is refused before the sandbox: pytest
looks it up as a fixture and errors the test at setup. Live (5b4880c0 API_REDDIT_AUDITF1 a2, 2026-11-05 07:29 UTC):
"fixture '_token_response' not found"; API_ALPACA_AUDITF1 had `patched` and `set_fake_client` the same way."""
from __future__ import annotations

from app.modules.engine_contracts import helpers_taken_as_fixtures

_TEST = '''import pytest


def _token_response():
    return {"access_token": "t", "expires_in": 3600}


@pytest.fixture
def client():
    return object()


async def test_token(_token_response, client, tmp_path):
    assert _token_response["access_token"]


class TestFetch:
    def test_fetch(self, client, _token_response):
        assert client
'''


def test_a_plain_helper_taken_as_a_fixture_is_named_in_each_test():
    got = helpers_taken_as_fixtures({"tests/test_paper_reddit.py": _TEST})
    assert [g.split(" -- ")[0] for g in got] == [
        "`tests/test_paper_reddit.py:13` `test_token` takes `_token_response` as a fixture, and `_token_response` is "
        "a plain function (line 4): pytest finds no fixture by that name and errors the test at setup",
        "`tests/test_paper_reddit.py:18` `test_fetch` takes `_token_response` as a fixture, and `_token_response` is "
        "a plain function (line 4): pytest finds no fixture by that name and errors the test at setup"]


def test_a_real_fixture_a_conftest_name_and_a_module_file_are_not():
    fixed = _TEST.replace("def _token_response():", "@pytest.fixture\ndef _token_response():")
    assert helpers_taken_as_fixtures({"tests/test_paper_reddit.py": fixed}) == []
    assert helpers_taken_as_fixtures({"tests/test_x.py": "def test_a(db, async_session):\n    pass\n"}) == []
    assert helpers_taken_as_fixtures({"app/modules/paper_x.py": _TEST}) == []
