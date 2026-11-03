"""§17.1897 — an attribute read on a standard-library module the file imports is checked against the real module
(`import time` then `time.min`: the `time` module has no `min`; `datetime.time` has), and a `from m import name` the
module lacks is an `ImportError` before any test runs. A name the file binds twice, or shadows with its own, is left
alone.

Live (follow-up 5b4880c0 T5 a3, 2026-11-03 23:03 UTC): `datetime.combine(trading_day, time.min, tzinfo=timezone.utc)`
under `import time` at line 53 -- "AttributeError: module 'time' has no attribute 'min'", one of the attempt's two
sandbox failures, readable by anyone with the same Python."""
from __future__ import annotations

from app.modules.engine_contracts import stdlib_attrs

PATH = "tests/test_paper_twelve_modules.py"


def test_a_missing_stdlib_attribute_and_a_missing_from_import_are_refused():
    test = '''# tests/test_paper_twelve_modules.py
import json
import os.path
import time
import collections as col
from datetime import date, datetime, timezone
from decimal import Decimal, Dozen

def test_day_start():
    day_start = datetime.combine(date(2026, 1, 1), time.min, tzinfo=timezone.utc)
    stamp = time.time()
    data = json.loads("{}")
    seen = col.OrderedDict()
    missing = json.parse("{}")
    here = os.path.join("a", "b")
'''
    got = stdlib_attrs({PATH: test})
    assert got == [
        "`tests/test_paper_twelve_modules.py` imports `Dozen` from `decimal`, which has no such name -- `ImportError` "
        "before any test runs",
        "`tests/test_paper_twelve_modules.py:10` reads `time.min`, but the `time` module (`import time`, line 4) has no "
        "`min` -- `AttributeError` the instant it runs; `datetime.time` has `.min`: `from datetime import time`",
        "`tests/test_paper_twelve_modules.py:14` reads `json.parse`, but the `json` module (`import json`, line 2) has no "
        "`parse` -- `AttributeError` the instant it runs"], got


def test_a_shadowed_or_twice_bound_name_and_a_third_party_module_are_left_alone():
    test = '''# tests/test_paper_twelve_modules.py
import time
import httpx
from datetime import time

def test_fine():
    assert time.min is not None            # `from datetime import time` shadows the module: the file's own binding
    assert httpx.Nothing is None           # not the standard library: not judged here

def test_local(time):
    return time.whatever
'''
    assert stdlib_attrs({PATH: test}) == []
    assert stdlib_attrs({"notes.md": "import time\\ntime.min"}) == []
