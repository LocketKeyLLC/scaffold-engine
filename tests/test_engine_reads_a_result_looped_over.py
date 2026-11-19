"""§17.1940 — a result the module loops over directly (`for row in await fetch_rows():`) is read: each item, as the
module uses it, is held against the fake the test hands in. Before, no use was recorded, so nothing checked it
(parked since §17.1937, 5b4880c0 DELIVERL2)."""
from __future__ import annotations

from app.modules import engine_contracts as ec

_MOD = '''from app.modules import paper_src


async def run() -> list[str]:
    out = []
    for row in await paper_src.fetch_rows():
        out.append(row.symbol.upper())
    return out
'''
_TEST = '''from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import paper_m


class FakeRow:
    def __init__(self, sym):
        self.{attr} = sym


@pytest.mark.asyncio
async def test_run():
    with patch("app.modules.paper_m.paper_src.fetch_rows", new=AsyncMock(return_value=[{item}])):
        assert await paper_m.run()
'''
_SRC = {"paper_src": "async def fetch_rows():\n    return []\n"}


def _found(attr: str, item: str) -> list[str]:
    files = {"app/modules/paper_m.py": _MOD, "tests/test_paper_m.py": _TEST.format(attr=attr, item=item)}
    return ec.wrong_fakes(files, {"paper_m": _MOD, **_SRC}, {"paper_m"})


def test_the_loop_is_a_use_of_each_item():
    u = ec._target_uses(_MOD, "paper_src", "fetch_rows")
    assert u is not None and u["called"]["await"] is True
    assert u["called"]["then"]["as"] == "list" and u["called"]["then"]["item"]["attrs"] == {"symbol"}


def test_a_fake_item_without_the_attribute_is_refused():
    got = _found("sym", "FakeRow('X')")
    assert len(got) == 1 and "whose first item is a `FakeRow` instance" in got[0] and "has no `symbol`" in got[0]


def test_a_fake_item_with_it_and_a_real_value_are_not():
    assert _found("symbol", "FakeRow('X')") == []
    assert _found("symbol", "date(2026, 11, 3)") == []         # a real value has what its type has
