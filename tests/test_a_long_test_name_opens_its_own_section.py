"""§17.1871 — pytest pads a failure's header to the terminal width, so a long test name gets one underscore on each
side; the section reader took three as the least. Live (5b4880c0 T5, 2026-10-30 04:16 UTC): two failures merged,
the FINRA module's missing name was pinned on the next test's deepest frame, and the planner added a fix step for a
module with nothing to fix (T5U1, three attempts). Also from that run: a mock configured through an alias
(`mock_client = cls.return_value; mock_client.submit_order = AsyncMock(…)`) read as unconfigured (six false
refusals); a call read twice after `raw = raw.decode()` rebinds its name; a patched name used only as the default
for a parameter the test passes (`http_get=`)."""
from __future__ import annotations

from app.modules import engine_source as es
from app.modules.engine_contracts import _default_for, _use_sites, wrong_fakes
from app.sandbox import engine_tests as et

MERGED = (
    "________ TestFinraDataIngestion.test_ingest_finra_file_uses_int_version ________\n"
    "tests/test_paper_twelve_modules.py:116: in test_ingest_finra_file_uses_int_version\n"
    "    result = await paper_finra_data_ingestion.ingest_finra_file(\n"
    "app/modules/paper_finra_data_ingestion.py:225: in _record_version\n"
    "    await paper_audit_trail_writer.write(\n"
    "E   AttributeError: module 'app.modules.paper_audit_trail_writer' has no attribute 'write'\n"
    "_ TestOperatorApprovalGate.test_handle_approval_answer_rejected_does_not_write_order _\n"
    "tests/test_paper_twelve_modules.py:138: in _insert_proposal\n"
    "    signal_id = await paper_audit_trail_writer.write_signal(\n"
    "app/modules/paper_audit_trail_writer.py:32: in write_signal\n"
    "    result = await db.execute(\n"
    "E   sqlalchemy.exc.IntegrityError: (ForeignKeyViolationError) insert or update on table \"signal\"\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_paper_twelve_modules.py::TestFinraDataIngestion::test_ingest_finra_file_uses_int_version\n"
    "2 failed, 1 passed in 3.0s\n")


def test_a_long_test_name_opens_its_own_section_so_the_defect_stays_in_its_own_frame():
    found = et.callee_defects(MERGED)
    assert found == [{"module": "paper_finra_data_ingestion",
                      "frame": "app/modules/paper_finra_data_ingestion.py:225 in _record_version",
                      "test": "TestFinraDataIngestion.test_ingest_finra_file_uses_int_version",
                      "cause": et.KNOWN_CAUSES[5][1]}]
    idx = et._failure_index([ln for ln in MERGED.splitlines() if ln.strip()])
    assert [x.split(":")[0] for x in idx] == ["TestFinraDataIngestion.test_ingest_finra_file_uses_int_version",
                                              "TestOperatorApprovalGate.test_handle_approval_answer_rejected_does_not_write_order"]


EXECUTOR = '''# app/modules/paper_order_executor.py
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def execute_order(approval: dict) -> dict:
    client = AlpacaPaperClient()
    try:
        order = await client.submit_order(approval["ticker"])
        return {"status": "executed", "id": order.id}
    finally:
        await client.close()
'''
FINRA = '''# app/modules/paper_finra_data_ingestion.py
import httpx

async def _download_file(url: str, *, http_get=None) -> str:
    getter = http_get or _default_http_get
    response = await getter(url)
    response.raise_for_status()
    return response.text

async def _default_http_get(url: str):
    client = httpx.AsyncClient()
    try:
        return await client.get(url)
    finally:
        await client.aclose()

async def ingest_finra_file(*, source: str, http_get=None) -> dict:
    content = await _download_file("u", http_get=http_get)
    return {"n": len(content)}
'''
TRACKER = '''# app/modules/paper_fill_tracker.py
import json

def parse_envelope(raw) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)
'''
MODULES = {m.split("\n", 1)[0][len("# app/modules/"):-3]: m.split("\n", 1)[1] for m in (EXECUTOR, FINRA, TRACKER)}
PATH = "tests/test_paper_twelve_modules.py"


def _files(test: str) -> dict[str, str]:
    return {PATH: "# " + PATH + "\n" + test}


def test_a_mock_configured_through_an_alias_is_configured():
    test = '''from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_order_executor import execute_order

async def test_executes():
    with patch("app.modules.paper_order_executor.AlpacaPaperClient", new_callable=MagicMock) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.submit_order = AsyncMock(return_value=MagicMock(id="broker-1"))
        mock_client.close = AsyncMock()
        await execute_order({"ticker": "SPY"})

async def test_unconfigured():
    with patch("app.modules.paper_order_executor.AlpacaPaperClient", new_callable=MagicMock) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.close = AsyncMock()
        await execute_order({"ticker": "SPY"})
'''
    found = wrong_fakes(_files(test), MODULES, set())
    assert len(found) == 1 and found[0].startswith("`tests/test_paper_twelve_modules.py:13` patches `AlpacaPaperClient`") \
        and "`submit_order()` returns a `MagicMock`" in found[0], found


def test_a_call_read_through_its_assignment_is_read_once():
    import ast
    tree = ast.parse(MODULES["paper_fill_tracker"])
    fn = es._local_functions(tree)["parse_envelope"]
    assert es._uses_text(es._uses(fn, "raw", es._local_functions(tree))) == "{decode()}"
    test = '''from unittest.mock import MagicMock
from app.modules.paper_fill_tracker import parse_envelope

def test_bytes():
    raw = MagicMock()
    raw.decode = MagicMock(return_value='{"a": 1}')
    parse_envelope(raw)
'''
    assert wrong_fakes(_files(test), MODULES, set()) == []


def test_a_patched_name_used_only_as_a_default_the_test_passes_is_not_reached():
    assert _use_sites(MODULES["paper_finra_data_ingestion"], "httpx", "AsyncClient") == {"_default_http_get"}
    assert _default_for(MODULES["paper_finra_data_ingestion"], "_default_http_get") == "http_get"
    assert _default_for(MODULES["paper_finra_data_ingestion"], "_download_file") is None      # called outright
    test = '''from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_finra_data_ingestion import ingest_finra_file

async def test_given():
    fake_http_get = AsyncMock(return_value=MagicMock(text="a|b"))
    with patch("app.modules.paper_finra_data_ingestion.httpx.AsyncClient", return_value=MagicMock()):
        await ingest_finra_file(source="short_interest", http_get=fake_http_get)

async def test_not_given():
    with patch("app.modules.paper_finra_data_ingestion.httpx.AsyncClient", return_value=MagicMock()):
        await ingest_finra_file(source="short_interest")
'''
    found = wrong_fakes(_files(test), MODULES, set())
    assert len(found) == 1 and found[0].startswith("`tests/test_paper_twelve_modules.py:11` patches `AsyncClient`"), found
