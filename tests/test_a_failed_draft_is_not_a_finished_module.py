"""§17.1872 — a failed step's draft is not a finished module; a fake `httpx.Response` carries its request; a bare mock
on a path the test never calls into is not refused.

Live (follow-up 5b4880c0 T5U2, 2026-10-30, the §17.1871 round): a4 was refused for `paper_audit_trail_writer.write(…)`
with the writer cited as "(step T5U1)" -- T5U1 had FAILED three times and its draft sat in `output_text`; the findings
check indexed every row of the job, so a failed draft defining a name the finished module lacks would have made the
name look present. a1 spent an attempt on nine sandbox failures from `httpx.Response(status_code, text=…)` built
without `request=`, which `_download_file`'s `raise_for_status()` refuses. And since §17.1870 a bare `MagicMock` client
patched into the order executor was refused as awaited when the test called the approval gate with a REJECTED
approval, which never reaches `execute_order`."""
from __future__ import annotations

import pytest

from app.modules.engine_contracts import responses_without_a_request, wrong_fakes

FINRA = '''# app/modules/paper_finra_data_ingestion.py
import httpx

async def _download_file(url: str, http_get=None) -> str:
    getter = http_get or _default_http_get
    response = await getter(url)
    response.raise_for_status()
    return response.text

async def _default_http_get(url: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(url)

async def ingest_finra_file(*, source: str, http_get=None) -> dict:
    return {"text": await _download_file("https://x", http_get=http_get)}
'''

LENIENT = '''# app/modules/paper_news_ingestion.py
async def fetch(url: str, http_get) -> str:
    response = await http_get(url)
    if response.status_code >= 400:
        raise RuntimeError(response.status_code)
    return response.text
'''


def _files(test: str) -> dict[str, str]:
    return {"tests/test_paper_finra_data_ingestion.py": test}


def test_a_response_built_without_its_request_is_refused_where_the_module_raises_for_status():
    test = '''# tests/test_paper_finra_data_ingestion.py
import httpx
from app.modules.paper_finra_data_ingestion import ingest_finra_file

def _response(status_code: int, text_content: str) -> httpx.Response:
    return httpx.Response(status_code, text=text_content)

async def test_it():
    async def fake_get(url):
        return _response(200, "a|b")
    await ingest_finra_file(source="short_interest", http_get=fake_get)
'''
    found = responses_without_a_request(_files(test), {"paper_finra_data_ingestion": FINRA})
    assert found == [
        "`tests/test_paper_finra_data_ingestion.py:6` builds an `httpx.Response(…)` without `request=`: "
        "`paper_finra_data_ingestion._download_file` calls `raise_for_status()` on the response it is given, and httpx "
        "refuses that on a response that never had a request (`RuntimeError: Cannot call raise_for_status as the "
        "request instance has not been set on this response`) -- pass `request=httpx.Request(\"GET\", url)` to "
        "`httpx.Response(…)`"], found
    # the same through `from httpx import Response`
    direct = test.replace("import httpx\n", "import httpx\nfrom httpx import Response\n").replace(
        "return httpx.Response(status_code", "return Response(status_code")
    assert len(responses_without_a_request(_files(direct), {"paper_finra_data_ingestion": FINRA})) == 1


def test_a_response_with_its_request_or_a_module_that_does_not_raise_for_status_passes():
    with_request = '''# tests/test_paper_finra_data_ingestion.py
import httpx
from app.modules.paper_finra_data_ingestion import ingest_finra_file

def _response(status_code: int, text_content: str) -> httpx.Response:
    return httpx.Response(status_code, text=text_content, request=httpx.Request("GET", "https://x"))
'''
    assert responses_without_a_request(_files(with_request), {"paper_finra_data_ingestion": FINRA}) == []
    set_after = '''# tests/test_paper_finra_data_ingestion.py
import httpx
from app.modules.paper_finra_data_ingestion import ingest_finra_file

def _response(status_code: int) -> httpx.Response:
    r = httpx.Response(status_code)
    r.request = httpx.Request("GET", "https://x")
    return r
'''
    assert responses_without_a_request(_files(set_after), {"paper_finra_data_ingestion": FINRA}) == []
    bare = '''# tests/test_paper_news_ingestion.py
import httpx
from app.modules.paper_news_ingestion import fetch

async def test_it():
    await fetch("u", lambda u: httpx.Response(200, text="x"))
'''
    # the module checks `status_code` itself: nothing to refuse; and a test that never names a raising module: nothing
    assert responses_without_a_request({"tests/test_paper_news_ingestion.py": bare}, {"paper_news_ingestion": LENIENT}) == []
    assert responses_without_a_request({"tests/test_paper_news_ingestion.py": bare},
                                       {"paper_news_ingestion": LENIENT, "paper_finra_data_ingestion": FINRA}) == []
    assert responses_without_a_request({"tests/test_x.py": "x = (\n"}, {"paper_finra_data_ingestion": FINRA}) == []


EXECUTOR = '''# app/modules/paper_order_executor.py
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def execute_order(approval: dict) -> dict:
    client = AlpacaPaperClient()
    try:
        order = await client.submit_order(symbol=approval["symbol"])
    finally:
        await client.close()
    return {"id": order.id}
'''

GATE = '''# app/modules/paper_operator_approval_gate.py
from app.modules import paper_order_executor

async def handle_approval_answer(approval: dict) -> dict:
    if approval.get("decision") != "approved":
        return {"status": "rejected"}
    return await paper_order_executor.execute_order(approval)
'''

MODULES = {"paper_order_executor": EXECUTOR, "paper_operator_approval_gate": GATE}


def _fakes(test: str) -> list[str]:
    return [f.split(" -- ")[0] for f in wrong_fakes({"tests/test_paper_twelve_modules.py": test}, MODULES, set())]


def test_a_bare_mock_on_a_path_the_test_never_calls_into_is_not_refused():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import MagicMock, patch
from app.modules import paper_operator_approval_gate

async def test_rejected_never_submits():
    with patch("app.modules.paper_order_executor.AlpacaPaperClient") as cls:
        out = await paper_operator_approval_gate.handle_approval_answer({"decision": "rejected"})
    assert out["status"] == "rejected"
'''
    assert _fakes(test) == []


def test_the_same_bare_mock_is_refused_when_the_test_calls_the_module_configures_it_or_asserts_on_it():
    direct = '''# tests/test_paper_twelve_modules.py
from unittest.mock import MagicMock, patch
from app.modules import paper_order_executor

async def test_direct():
    with patch("app.modules.paper_order_executor.AlpacaPaperClient") as cls:
        await paper_order_executor.execute_order({"symbol": "AAPL"})
'''
    assert _fakes(direct) == [
        "`tests/test_paper_twelve_modules.py:6` patches `AlpacaPaperClient` on `paper_order_executor` with a fake whose "
        "call returns a fake whose `submit_order()` returns a `MagicMock` (line 6), which the module awaits"]
    asserted = '''# tests/test_paper_twelve_modules.py
from unittest.mock import MagicMock, patch
from app.modules import paper_operator_approval_gate

async def test_approved_submits():
    with patch("app.modules.paper_order_executor.AlpacaPaperClient") as cls:
        await paper_operator_approval_gate.handle_approval_answer({"decision": "approved", "symbol": "AAPL"})
    cls.return_value.submit_order.assert_awaited_once()
'''
    assert len(_fakes(asserted)) == 1, _fakes(asserted)
    configured = '''# tests/test_paper_twelve_modules.py
from unittest.mock import MagicMock, patch
from app.modules import paper_operator_approval_gate

async def test_approved_submits():
    with patch("app.modules.paper_order_executor.AlpacaPaperClient") as cls:
        cls.return_value = MagicMock()
        await paper_operator_approval_gate.handle_approval_answer({"decision": "approved", "symbol": "AAPL"})
'''
    assert len(_fakes(configured)) == 1, _fakes(configured)


WRITER = '''# app/modules/paper_audit_trail_writer.py
async def write_signal(db, *, ingested_item_id: int) -> int:
    return 1
'''

DRAFT = '''# app/modules/paper_audit_trail_writer.py
async def write_signal(db, *, ingested_item_id: int) -> int:
    return 1

async def write(db, table: str, row: dict) -> int:
    return 1
'''

FIX = '''# app/modules/paper_finra_data_ingestion.py
from app.modules import paper_audit_trail_writer

async def record(db) -> int:
    return await paper_audit_trail_writer.write(db, "signal", {})
'''


@pytest.mark.integration
async def test_a_failed_steps_draft_does_not_stand_in_for_the_finished_module():
    """The wiring: `engine_findings` reads the finished writer (T4), not the failed fix step's draft (T5U1) that
    defines the missing name."""
    import json
    import uuid

    from app.database import async_session
    from app.modules import engine_source, node_editor
    text = node_editor.text
    jid = str(uuid.uuid4())
    brief = {"title": "lab", "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}
    output = f"```python\n{FIX}```"
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                              "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(brief)})
        for i, (k, st, prompt, out) in enumerate([
                ("T4", "done", "Python module `paper_audit_trail_writer.py`.", f"```python\n{WRITER}```"),
                ("T5U1", "failed", "Fix `paper_audit_trail_writer.py` in place.", f"```python\n{DRAFT}```"),
                ("T5U2", "running", "Fix `paper_finra_data_ingestion.py` in place.", None)]):
            await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, output_text, tool, "
                "execution_order) VALUES (:j, :k, :k, :s, :p, :o, 'CodeGen', :n)"),
                {"j": jid, "k": k, "s": st, "p": prompt, "o": out, "n": i})
        await db.commit()
    try:
        async with async_session() as db:
            found = await engine_source.engine_findings(db, jid, "T5U2", output)
        assert any(f.startswith("`paper_audit_trail_writer.write(…)`: `paper_audit_trail_writer` (step T4) has no `write`")
                   for f in found), found
        assert not any("T5U1" in f for f in found), found
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
