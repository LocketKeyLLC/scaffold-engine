"""§17.1951 — a test file that defines one top-level name twice, with different bodies, is refused: the later one
replaces the earlier for every test in the file. The 11-03 "spy-test regression" (5b4880c0 T5 a2) fits only this;
and the gate's first run over the repo's own tests found one: a test never collected since it was written."""
from __future__ import annotations

from app.modules.engine_contracts import redefined_test_names

_FILE = '''import pytest
from decimal import Decimal


def _prices():
    return [100.0, 110.0, 99.0]


class TestSpy:
    def test_drawdown(self):
        assert min(_prices()) == 99.0


def _prices():
    return [Decimal("100"), Decimal("101")]


class TestOperatorView:
    def test_view(self):
        assert _prices()
'''


def test_a_second_body_under_one_name_is_named():
    got = redefined_test_names({"tests/test_paper_twelve_modules.py": _FILE})
    assert [g.split(" -- ")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:14` defines `_prices` again (first at line 5), with another body: the "
        "later one replaces the earlier for every test in the file, those written for the first too"]


def test_the_same_body_twice_or_distinct_names_are_not():
    same = _FILE.replace('    return [Decimal("100"), Decimal("101")]', "    return [100.0, 110.0, 99.0]")
    assert redefined_test_names({"tests/test_x.py": same}) == []
    renamed = _FILE.replace("def _prices():\n    return [Decimal", "def _decimal_prices():\n    return [Decimal")
    assert redefined_test_names({"tests/test_x.py": renamed}) == []
    assert redefined_test_names({"app/modules/paper_x.py": _FILE}) == []
