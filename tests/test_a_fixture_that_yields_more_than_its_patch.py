"""§17.1937 — a fixture that patches `as m` and yields something else (`yield mock_a, mock_b`) does not hand the test
the mock: the mock is `m`, read in the fixture's own scope (what it set on `m.return_value`). Live (follow-up 5b4880c0
DELIVERL2, 2026-11-05): two `new_callable=AsyncMock` patches, each set up, the pair yielded -- refused as "patches
`propose_trades` ... with a tuple", the step's last attempt spent."""
from __future__ import annotations

from app.modules.engine_contracts import wrong_fakes

MOD = '''# app/modules/paper_bt.py
from app.modules.paper_tp import propose_trades


async def run(db):
    proposals = await propose_trades(db)
    out = []
    for p in proposals:
        out.append(p["ticker"])
    return out
'''
TP = "async def propose_trades(db):\n    return []\n"


def _test(value: str) -> str:
    return f'''# tests/test_paper_bt.py
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.paper_bt import run


@pytest.fixture
def patched():
    with patch("app.modules.paper_bt.propose_trades", new_callable=AsyncMock) as mock_propose, patch(
        "app.modules.paper_bt.other", new_callable=AsyncMock, create=True
    ) as mock_other:
        mock_propose.return_value = {value}
        yield mock_propose, mock_other


async def test_run(patched):
    assert await run(None) is not None
'''


def test_the_pair_yielded_is_not_the_patch_and_the_mock_set_up_in_the_fixture_is_read():
    mods = {"paper_bt": MOD, "paper_tp": TP}
    assert wrong_fakes({"tests/test_paper_bt.py": _test('[{"ticker": "SPY"}]')}, mods, {"paper_bt"}) == []
    bad = wrong_fakes({"tests/test_paper_bt.py": _test('{"ticker": "SPY"}')}, mods, {"paper_bt"})
    assert len(bad) == 1 and "patches `propose_trades` on `paper_bt`" in bad[0] and "tuple" not in bad[0]
