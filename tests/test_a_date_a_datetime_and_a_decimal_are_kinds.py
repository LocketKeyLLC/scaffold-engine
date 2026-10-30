"""§17.1865 — a date, a datetime and a Decimal are kinds: shown in the index, checked at the call, through a data
class's constructor and through the keys of a dict parameter.

Live (follow-up 5b4880c0 T5, every attempt of three rounds, 2026-10-29): the tests passed `release_date="20261015"`
where `ingest_finra_file` takes a `date` (`'str' object has no attribute 'strftime'`), built a
`FinraRecord(release_date="2026-10-15")` (`'str' object has no attribute 'isoformat'`), and handed
`{"quantity": "1"}` where the approval gate formats `proposal["quantity"]` as a Decimal (`Unknown format code 'f'
for object of type 'str'`). The §17.1761 table knew six kinds; the index showed these parameters bare; a data class's
constructor carried no kinds at all; and a call through `from app.modules import paper_x` was never checked."""
from __future__ import annotations

import ast

from app.modules import engine_source as es

FINRA = '''# app/modules/paper_finra_data_ingestion.py
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional

@dataclass(frozen=True)
class FinraRecord:
    external_id: str
    release_date: date
    settlement_date: date | None
    fields: dict[str, Any] = field(default_factory=dict)

    def payload(self):
        return {"release_date": self.release_date.isoformat()}

async def ingest_finra_file(*, source: str, release_date: date, version: int = 1, http_get=None) -> dict:
    return {"url": release_date.strftime("%Y%m%d")}

def _format_decimal(value: Optional[Decimal]) -> str:
    return f"{value:.6f}"

async def request_approval(proposal: dict[str, Any], *, db=None) -> dict:
    return {"text": _format_decimal(proposal.get("quantity")), "id": proposal.get("proposal_id")}
'''


def _ann(src: str) -> ast.AST:
    return ast.parse(src, mode="eval").body


def test_the_three_kinds_are_read_from_annotations_with_what_each_admits():
    assert es._kinds(_ann("date")) == frozenset({"date", "datetime"})           # a datetime is a date
    assert es._kinds(_ann("datetime")) == frozenset({"datetime"})
    assert es._kinds(_ann("Decimal")) == frozenset({"Decimal", "int"})          # an int is a Decimal
    assert es._kinds(_ann("datetime.date")) == frozenset({"date", "datetime"})
    assert es._kinds(_ann("Optional[decimal.Decimal]")) == frozenset({"Decimal", "int", "None"})
    # one spelling: what a kind admits besides itself is not spelled out
    assert es._kinds_text(es._kinds(_ann("date"))) == "date"
    assert es._kinds_text(es._kinds(_ann("Optional[Decimal]"))) == "Decimal | None"
    assert es._kinds_text(es._kinds(_ann("float"))) == "float"


def test_a_literal_date_datetime_or_decimal_has_its_kind():
    assert es._literal_kind(_ann("date(2026, 10, 15)")) == "date"
    assert es._literal_kind(_ann("datetime.date(2026, 10, 15)")) == "date"
    assert es._literal_kind(_ann("datetime.now(timezone.utc)")) == "datetime"
    assert es._literal_kind(_ann("date.today()")) == "date"
    assert es._literal_kind(_ann('Decimal("1")')) == "Decimal"
    assert es._literal_kind(_ann('"2026-10-15"')) == "str"
    assert es._literal_kind(_ann("make_proposal()")) is None


def _refs(test: str) -> list[str]:
    defs = es.sibling_definitions([{"node_key": "T1", "status": "done", "output_text": "```python\n" + FINRA + "```"}])
    return es.sibling_refs("```python\n" + test + "```", defs)


def test_the_live_strings_are_refused_through_the_function_the_constructor_and_the_dict_key():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules import paper_finra_data_ingestion

async def test_it():
    rec = paper_finra_data_ingestion.FinraRecord(external_id="x", release_date="2026-10-15", settlement_date=None)
    await paper_finra_data_ingestion.ingest_finra_file(source="short_interest", release_date="20261015")
    proposal = {"id": 1, "quantity": "1", "proposal_id": 1}
    await paper_finra_data_ingestion.request_approval(proposal, db=None)
'''
    found = _refs(test)
    assert any("`tests/test_paper_twelve_modules.py:5` calls `FinraRecord(…)`: `release_date` is `date`; it was given a `str`" in f
               for f in found), found
    assert any("`tests/test_paper_twelve_modules.py:6` calls `ingest_finra_file(…)`: `release_date` is `date`; it was given a `str`" in f
               for f in found), found
    # the dict key: the kind the function uses the value as, through its own `_format_decimal(value: Optional[Decimal])`
    assert any('`tests/test_paper_twelve_modules.py:8` calls `request_approval(…)`: `proposal`["quantity"] is `Decimal | None`; '
               "it was given a `str`" in f for f in found), found
    # and the signature names the kinds, the constructor's too
    assert any("release_date: date, settlement_date: date | None, fields: dict = …" in f for f in found)
    assert any("proposal: dict{proposal_id, quantity: Decimal | None}" in f for f in found)


def test_what_a_kind_admits_passes():
    test = '''# tests/test_paper_twelve_modules.py
from datetime import date, datetime, timezone
from decimal import Decimal
from app.modules import paper_finra_data_ingestion

async def test_it():
    paper_finra_data_ingestion.FinraRecord(external_id="x", release_date=date(2026, 10, 15), settlement_date=datetime.now(timezone.utc))
    await paper_finra_data_ingestion.ingest_finra_file(source="short_interest", release_date=datetime.now(timezone.utc))
    await paper_finra_data_ingestion.request_approval({"quantity": 1, "proposal_id": 1}, db=None)
    await paper_finra_data_ingestion.request_approval({"quantity": Decimal("1"), **extra}, db=None)
'''
    assert _refs(test) == []
