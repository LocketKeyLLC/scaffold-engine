"""§17.1863 — a secret named by a constant is mocked by its name.

Live (follow-up 5b4880c0 T5, 2026-10-29, attempt 2): the 13F ingester reads `values_for(db, [SEC_USER_AGENT_SECRET])`
with `SEC_USER_AGENT_SECRET = "SEC_CONTACT_EMAIL"`. The test patched `values_for` on that module -- the §17.1857 gate
was satisfied -- to return `{"SEC_USER_AGENT": "test@example.com"}`, a guess from the constant's name; the module read
`secrets.get(SEC_USER_AGENT_SECRET)`, found nothing, and refused to run. Nothing the step was shown named the secret."""
from __future__ import annotations

import inspect

from app.modules import engine_source as es
from app.modules.engine_contracts import _values_for_mocks, wrong_secret_mocks

HOLDINGS = '''# app/modules/paper_13f_holdings_ingestion.py
import httpx
from app.modules.runner_secrets import values_for
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

SEC_USER_AGENT_SECRET = "SEC_CONTACT_EMAIL"


async def ingest_13f_holdings(cik: str, *, db, client) -> int:
    secrets = await values_for(db, [SEC_USER_AGENT_SECRET])
    if not secrets.get(SEC_USER_AGENT_SECRET):
        raise RuntimeError("Missing operator secret")
    return 0


async def priced(db):
    c = AlpacaPaperClient()
    return await c.get_account(db)


def parse_rows(rows):
    return list(rows)
'''
CLIENT = '''# app/modules/paper_alpaca_paper_client.py
from app.modules.runner_secrets import values_for

KEY = "ALPACA_API_KEY_ID"


class AlpacaPaperClient:
    def __init__(self, *, base_url: str = "x"):
        self.base_url = base_url

    async def get_account(self, db):
        s = await values_for(db, [KEY, "ALPACA_API_SECRET_KEY"])
        return s
'''
ROWS = [{"node_key": "T8", "status": "done", "output_text": "```python\n" + HOLDINGS + "```"},
        {"node_key": "T9", "status": "done", "output_text": "```python\n" + CLIENT + "```"}]
MODULES = {"paper_13f_holdings_ingestion": HOLDINGS, "paper_alpaca_paper_client": CLIENT}


def _test(body: str) -> dict[str, str]:
    return {"tests/test_paper_twelve_modules.py": (
        "# tests/test_paper_twelve_modules.py\nimport pytest\nfrom unittest.mock import AsyncMock, patch\n"
        "from app.modules.paper_13f_holdings_ingestion import ingest_13f_holdings, priced, parse_rows\n\n" + body)}


def test_the_index_names_the_secrets_a_function_reads_by_their_own_names():
    defs = es.sibling_definitions(ROWS)
    assert defs["paper_13f_holdings_ingestion"]["secrets"] == {
        "ingest_13f_holdings": ["SEC_CONTACT_EMAIL"],                          # named by a constant
        "priced": ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"]}              # through another module's class
    assert defs["paper_alpaca_paper_client"]["secrets"] == {"AlpacaPaperClient": ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"]}
    r = es._render_job_modules(defs, "")
    assert "ingest_13f_holdings(cik: str, *, db, client) [secrets: SEC_CONTACT_EMAIL]" in r
    assert "async priced(db) [secrets: ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY]" in r
    assert "parse_rows(rows);" in r and "parse_rows(rows) [" not in r
    assert "class AlpacaPaperClient(*, base_url: str = …) {async get_account(db)} [secrets: ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY]" in r
    assert "`[secrets: A, B]` means the function reads those operator secrets through `values_for`" in r
    # the carried inventory too, for what the module reads itself
    api = es._public_api(HOLDINGS)
    assert "ingest_13f_holdings(cik: str, *, db, client) [secrets: SEC_CONTACT_EMAIL]" in api
    assert "parse_rows(rows)" in api and "parse_rows(rows) [" not in api


def test_the_mock_reader_sees_each_shape_of_stand_in():
    code = '''# tests/test_x.py
from unittest.mock import AsyncMock, patch
from app.modules import paper_13f_holdings_ingestion as mod

def test_a(monkeypatch):
    with patch("app.modules.paper_13f_holdings_ingestion.values_for", new=AsyncMock(return_value={"A": 1, "B": 2})):
        pass
    with patch("app.modules.paper_13f_holdings_ingestion.values_for", AsyncMock(return_value={"C": 1})):
        pass
    with patch("app.modules.paper_13f_holdings_ingestion.values_for", return_value={"D": 1}):
        pass
    monkeypatch.setattr(mod, "values_for", AsyncMock(return_value={"E": 1}))
    monkeypatch.setattr("app.modules.runner_secrets.values_for", lambda db, wanted: {"F": 1})
    with patch("app.modules.paper_13f_holdings_ingestion.values_for", new=AsyncMock(side_effect=fake)):
        pass
    with patch("app.modules.paper_13f_holdings_ingestion.fetch", new=AsyncMock(return_value={"G": 1})):
        pass
'''
    got = _values_for_mocks(code)
    assert [(m, k) for m, _ln, k in got] == [
        ("paper_13f_holdings_ingestion", ["A", "B"]), ("paper_13f_holdings_ingestion", ["C"]),
        ("paper_13f_holdings_ingestion", ["D"]), ("paper_13f_holdings_ingestion", ["E"]),
        ("runner_secrets", ["F"]), ("paper_13f_holdings_ingestion", None)]


def test_the_live_guess_is_refused_and_the_right_name_passes():
    live = _test('''@pytest.mark.asyncio
async def test_13f_holdings_ingestion_ingests():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new=AsyncMock(return_value={"SEC_USER_AGENT": "test@example.com"}),
    ):
        result = await ingest_13f_holdings(cik="1", db=None, client=None)
    assert result == 0
''')
    found = wrong_secret_mocks(live, MODULES, set())
    assert found == ["`tests/test_paper_twelve_modules.py:8` patches `values_for` on `paper_13f_holdings_ingestion` to "
                     "return `SEC_USER_AGENT`; `ingest_13f_holdings` reads `SEC_CONTACT_EMAIL` (the value of its constant "
                     "`SEC_USER_AGENT_SECRET`) and refuses to run without it -- return a test value under that name: "
                     '`{"SEC_CONTACT_EMAIL": …}`']
    assert found[0].startswith("`tests/")                                       # §17.1859: a redraw, not the attempt
    right = _test('''@pytest.mark.asyncio
async def test_13f_holdings_ingestion_ingests():
    with patch("app.modules.paper_13f_holdings_ingestion.values_for",
               new=AsyncMock(return_value={"SEC_CONTACT_EMAIL": "test@example.com"})):
        assert await ingest_13f_holdings(cik="1", db=None, client=None) == 0
''')
    assert wrong_secret_mocks(right, MODULES, set()) == []


def test_a_stand_in_counts_only_for_the_calls_in_its_scope_and_the_names_that_reach_it():
    # a patch on the client module answers for the client's secrets only; `priced` reaches them through the class
    two = _test('''@pytest.mark.asyncio
async def test_priced():
    with patch("app.modules.paper_alpaca_paper_client.values_for", new=AsyncMock(return_value={"ALPACA_API_KEY_ID": "k"})):
        await priced(None)

@pytest.mark.asyncio
async def test_rows():
    with patch("app.modules.paper_13f_holdings_ingestion.values_for", new=AsyncMock(return_value={})):
        assert parse_rows([]) == []
''')
    found = wrong_secret_mocks(two, MODULES, set())
    assert len(found) == 1 and found[0].startswith("`tests/test_paper_twelve_modules.py:8` patches `values_for` on "
                                                   "`paper_alpaca_paper_client` to return `ALPACA_API_KEY_ID`; `priced` reads "
                                                   "`ALPACA_API_SECRET_KEY` and refuses")
    # a stand-in on `runner_secrets` answers for every reader; a fixture's counts for the whole file
    fx = _test('''@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setattr("app.modules.runner_secrets.values_for", AsyncMock(return_value={"SEC_CONTACT_EMAIL": "e"}))

@pytest.mark.asyncio
async def test_priced():
    await priced(None)
''')
    found = wrong_secret_mocks(fx, MODULES, set())
    assert len(found) == 1 and "`runner_secrets`" in found[0] and "`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`" in found[0]
    assert found[0].endswith('`{"ALPACA_API_KEY_ID": …, "ALPACA_API_SECRET_KEY": …}`')
    # a return the engine cannot read is not judged
    opaque = _test('''@pytest.mark.asyncio
async def test_it():
    with patch("app.modules.paper_13f_holdings_ingestion.values_for", new=AsyncMock(side_effect=fake_values)):
        await ingest_13f_holdings(cik="1", db=None, client=None)
''')
    assert wrong_secret_mocks(opaque, MODULES, set()) == []


def test_the_gate_runs_in_engine_findings_and_the_redraw_ask_names_the_rule():
    from app.modules import execution_agent
    assert "wrong_secret_mocks(files_of" in inspect.getsource(es.engine_findings)
    assert "returning a test value under each secret's " in inspect.getsource(execution_agent)
