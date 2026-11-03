"""§17.1890 — the last parked items: a method of a fake class the module reads as a value and never calls (a bound
method where the module wanted the text); a `@property` on a fake is its value; a returned text compared with a
date, a decimal or a number (and the other way round), through a job class's constructor and its method or property.

Live (follow-up 5b4880c0 T5, 2026-11-02 21:01–21:23 UTC): a4 `class FakeResponse: def text(self): …` handed to the
FINRA ingester, which reads `response.text` -- "initial_value must be str or None, not method", two tests; a1
`payload = record.payload()` then `assert payload["release_date"] == date(2026, 10, 27)` where the dataclass returns
`self.release_date.isoformat()` -- "assert '2026-10-27' == datetime.date(2026, 10, 27)". Parked three rounds as
'data-content assertions' and 'a fake's `.text` as a method'."""
from __future__ import annotations

from app.modules.engine_contracts import returned_compares, wrong_fakes

PATH = "tests/test_paper_twelve_modules.py"

FINRA = '''# app/modules/paper_finra_data_ingestion.py
import csv
import io
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class FinraRecord:
    external_id: str
    release_date: date
    fields: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {"external_id": self.external_id, "release_date": self.release_date.isoformat(),
                "count": len(self.fields), **self.fields}

    @property
    def key(self) -> str:
        return f"finra:{self.external_id}"


async def ingest_finra_file(*, source: str, http_get) -> dict:
    response = await http_get("https://x")
    response.raise_for_status()
    content = response.text
    reader = csv.reader(io.StringIO(content), delimiter="|")
    return {"ingested_items": sum(1 for row in reader if len(row) >= 5)}
'''

MODULES = {"paper_finra_data_ingestion": FINRA}


def test_a_method_the_module_reads_as_a_value_is_refused_and_a_property_is_its_value():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock
from app.modules.paper_finra_data_ingestion import ingest_finra_file

class FakeResponse:
    def __init__(self, body: str):
        self._body = body

    def text(self):
        return self._body

    def raise_for_status(self):
        pass

class FakePropResponse:
    def __init__(self, body: str):
        self._body = body

    @property
    def text(self):
        return self._body

    def raise_for_status(self):
        pass

async def test_method_text():
    http_get = AsyncMock(return_value=FakeResponse("20261025|AAPL|100|10|2.5\\n"))
    result = await ingest_finra_file(source="short_interest", http_get=http_get)

async def test_property_text():
    http_get = AsyncMock(return_value=FakePropResponse("20261025|AAPL|100|10|2.5\\n"))
    result = await ingest_finra_file(source="short_interest", http_get=http_get)
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert [g.split(" -- give it")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:28` hands `ingest_finra_file(…)` `http_get` = a fake whose call returns a "
        "fake whose `.text` is a method `text` (line 9) the module never calls -- it reads it as a value: make `text` an "
        "attribute set on the instance (`self.text = …`), or a `@property`"], got
    # the property's value is read: a dict there would be refused where the module splits text
    fixed = test.replace("    def text(self):\n        return self._body\n\n    def raise_for_status(self):\n        pass\n\nclass FakePropResponse",
                         "    @property\n    def text(self):\n        return self._body\n\n    def raise_for_status(self):\n        pass\n\nclass FakePropResponse")
    assert wrong_fakes({PATH: fixed}, MODULES, set()) == []


def test_a_returned_text_compared_with_a_date_or_a_number_is_refused_through_a_classs_method():
    test = '''# tests/test_paper_twelve_modules.py
from datetime import date
from decimal import Decimal
from app.modules.paper_finra_data_ingestion import FinraRecord

def test_payload():
    record = FinraRecord(external_id="finra-1", release_date=date(2026, 10, 27), fields={"short_interest": 1234})
    payload = record.payload()
    assert payload["external_id"] == "finra-1"
    assert payload["release_date"] == date(2026, 10, 27)
    assert payload["count"] == "1"
    assert payload["count"] == 1
    assert payload["release_date"] == "2026-10-27"
    assert payload["short_interest"] == 1234
'''
    got = returned_compares({PATH: test}, MODULES)
    assert got == [
        "`tests/test_paper_twelve_modules.py:10` asserts `payload['release_date'] == date(2026, 10, 27)`, but "
        "`FinraRecord.payload` (`paper_finra_data_ingestion`) returns `self.release_date.isoformat()` under "
        "`release_date` -- a `str`, never equal to date(2026, 10, 27): compare its text (`date(2026, 10, 27).isoformat()`)",
        "`tests/test_paper_twelve_modules.py:11` asserts `payload['count'] == '1'`, but `FinraRecord.payload` "
        "(`paper_finra_data_ingestion`) returns `len(self.fields)` under `count` -- a number, never equal to '1': "
        "compare numbers (`Decimal(…)` / `float(…)`)"], got      # `short_interest` comes through `**self.fields`: unread
