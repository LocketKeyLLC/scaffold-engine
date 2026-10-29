"""§17.1857 — a test that runs a module that reads an operator secret, without mocking the secrets reader, is refused
before the sandbox; and the sandbox names the secret next to the error.

Live (follow-up 5b4880c0, 2026-10-28): T5's tests ran the EDGAR ingester for real -- "RuntimeError: Missing operator
secret SEC_CONTACT_EMAIL; refusing to run" -- in two rounds, and T8's operator view built the broker client for real
-- "AlpacaSecretsMissingError: Missing Alpaca paper trading secrets: ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY". The
§17.1839 block names `values_for` as a mock point; neither test used it, and nothing refused the test before it ran.
"""
from __future__ import annotations

from app.modules.engine_contracts import runs_secrets_for_real, secrets_read
from app.sandbox import engine_tests as et

EDGAR = '''import httpx
from app.modules.runner_secrets import values_for

SEC_UA = "SEC_USER_AGENT"


async def _contact(db):
    secrets = await values_for(db, ["SEC_CONTACT_EMAIL"])
    return secrets["SEC_CONTACT_EMAIL"]


async def ingest_form_4_filings(*, db, cik):
    email = await _contact(db)
    return {"cik": cik, "email": email}


async def ingest_form_4_for_tickers(*, db, tickers):
    return [await ingest_form_4_filings(db=db, cik=t) for t in tickers]


def parse_form_4(xml):
    return {}
'''
CLIENT = '''import httpx
from app.modules.runner_secrets import values_for

KEY_NAME = "ALPACA_API_KEY_ID"
SECRET_NAME = "ALPACA_API_SECRET_KEY"


async def _get_credentials(db):
    wanted = [KEY_NAME, SECRET_NAME]
    values = await values_for(db, wanted)
    return values


class AlpacaPaperClient:
    def __init__(self, *, base_url="x"):
        self.base_url = base_url

    @classmethod
    async def from_secrets(cls, db):
        await _get_credentials(db)
        return cls()

    async def _request(self, db):
        self._credentials = await _get_credentials(db)

    async def get_account(self, db):
        await self._request(db)
        return {}

    def base(self):
        return self.base_url
'''
VIEW = '''# app/modules/paper_operator_view.py
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient
from app.modules.paper_edgar_form_4_ingestion import parse_form_4


async def build_operator_view(db):
    client = await AlpacaPaperClient.from_secrets(db)
    return {"client": client}


async def spy_benchmark(db):
    client = AlpacaPaperClient(base_url="http://x")
    return await client.get_account(db)


async def just_built():
    client = AlpacaPaperClient(base_url="http://x")
    return client.base()
'''
MODULES = {"paper_edgar_form_4_ingestion": EDGAR, "paper_alpaca_paper_client": CLIENT}


def _files(test: str) -> dict[str, str]:
    return {"app/modules/paper_operator_view.py": VIEW, "tests/test_paper_twelve_modules.py": test}


def test_the_secrets_a_function_or_class_reads_are_followed_through_the_module():
    assert secrets_read(MODULES, "paper_edgar_form_4_ingestion", "ingest_form_4_filings") == \
        {"SEC_CONTACT_EMAIL": "paper_edgar_form_4_ingestion"}
    assert secrets_read(MODULES, "paper_edgar_form_4_ingestion", "ingest_form_4_for_tickers") == \
        {"SEC_CONTACT_EMAIL": "paper_edgar_form_4_ingestion"}                                   # two hops
    assert secrets_read(MODULES, "paper_edgar_form_4_ingestion", "parse_form_4") == {}
    assert secrets_read(MODULES, "paper_alpaca_paper_client", "AlpacaPaperClient.from_secrets") == \
        {"ALPACA_API_KEY_ID": "paper_alpaca_paper_client", "ALPACA_API_SECRET_KEY": "paper_alpaca_paper_client"}
    assert secrets_read(MODULES, "paper_alpaca_paper_client", "AlpacaPaperClient") == {}      # the constructor: none
    assert secrets_read(MODULES, "paper_alpaca_paper_client", "AlpacaPaperClient.get_account") == \
        {"ALPACA_API_KEY_ID": "paper_alpaca_paper_client", "ALPACA_API_SECRET_KEY": "paper_alpaca_paper_client"}
    assert secrets_read(MODULES, "paper_alpaca_paper_client", "AlpacaPaperClient.base") == {}
    both = {**MODULES, "paper_operator_view": VIEW}
    assert secrets_read(both, "paper_operator_view", "spy_benchmark") == \
        {"ALPACA_API_KEY_ID": "paper_alpaca_paper_client", "ALPACA_API_SECRET_KEY": "paper_alpaca_paper_client"}
    assert secrets_read(both, "paper_operator_view", "just_built") == {}            # built, never asked
    assert secrets_read(MODULES, "paper_alpaca_paper_client", "nope") == {}
    # the chain stops at a name the test patched: the view's client stood in for
    assert secrets_read({**MODULES, "paper_operator_view": VIEW}, "paper_operator_view", "build_operator_view",
                        patched=frozenset({("paper_operator_view", "AlpacaPaperClient")})) == {}


def test_the_live_edgar_run_is_refused_with_the_secret_and_the_patch_target():
    test = '''from app.modules.paper_edgar_form_4_ingestion import ingest_form_4_filings, parse_form_4

async def test_x(db):
    assert parse_form_4("<xml/>") == {}
    result = await ingest_form_4_filings(db=db, cik="0000320193")
'''
    found = runs_secrets_for_real(_files(test), MODULES, {"paper_operator_view"})
    assert found == [
        "`tests/test_paper_twelve_modules.py:5` runs `ingest_form_4_filings` (`paper_edgar_form_4_ingestion`) for real, "
        "and it reads the operator secret `SEC_CONTACT_EMAIL` through `values_for` (`paper_edgar_form_4_ingestion`) -- "
        "the sandbox has no secrets, so the code refuses to run (§17.1776). Patch "
        "`app.modules.paper_edgar_form_4_ingestion.values_for` (where the module looks it up) to return a test value "
        "for each name (`SEC_CONTACT_EMAIL`); mock the network it then reaches as well"]


def test_a_mocked_reader_a_patched_function_and_the_steps_own_module_are_read():
    # the step's own module reaches the client, which reads two secrets: the test runs the step's function
    own = '''from app.modules import paper_operator_view as view

async def test_x(db):
    await view.build_operator_view(db)
'''
    found = runs_secrets_for_real(_files(own), MODULES, {"paper_operator_view"})
    assert len(found) == 1 and found[0].startswith(
        "`tests/test_paper_twelve_modules.py:4` runs `build_operator_view` (`paper_operator_view`) for real, and it "
        "reads the operator secrets `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` through `values_for` "
        "(`paper_alpaca_paper_client`) -- ")
    # `values_for` patched anywhere in the file: nothing to say
    mocked = '''from unittest.mock import AsyncMock, patch
from app.modules.paper_edgar_form_4_ingestion import ingest_form_4_filings

async def test_x(db):
    with patch("app.modules.paper_edgar_form_4_ingestion.values_for", new=AsyncMock(return_value={"SEC_CONTACT_EMAIL": "a@b"})):
        await ingest_form_4_filings(db=db, cik="1")
'''
    assert runs_secrets_for_real(_files(mocked), MODULES, {"paper_operator_view"}) == []
    # patched on `runner_secrets` itself: every reader is covered
    at_source = mocked.replace("app.modules.paper_edgar_form_4_ingestion.values_for", "app.modules.runner_secrets.values_for")
    assert runs_secrets_for_real(_files(at_source), MODULES, {"paper_operator_view"}) == []
    # live (T5 attempt 4): patched on ANOTHER module's `values_for` only -- the EDGAR ingester still runs for real
    elsewhere = mocked.replace("app.modules.paper_edgar_form_4_ingestion.values_for", "app.modules.paper_alpaca_paper_client.values_for")
    assert len(runs_secrets_for_real(_files(elsewhere), MODULES, {"paper_operator_view"})) == 1
    # the function itself patched (it reaches the network: allowed): it does not run for real
    patched = '''from unittest.mock import patch
from app.modules.paper_edgar_form_4_ingestion import ingest_form_4_filings

async def test_x(db):
    with patch("app.modules.paper_edgar_form_4_ingestion.ingest_form_4_filings"):
        await ingest_form_4_filings(db=db, cik="1")
'''
    assert runs_secrets_for_real(_files(patched), MODULES, {"paper_operator_view"}) == []
    # the client built for real (its constructor reads nothing) and the chain cut by a patch: nothing to say
    built = '''from unittest.mock import patch
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient
from app.modules import paper_operator_view as view

async def test_x(db):
    client = AlpacaPaperClient(base_url="http://x")
    with patch("app.modules.paper_operator_view.AlpacaPaperClient"):
        await view.build_operator_view(db)
'''
    assert runs_secrets_for_real(_files(built), MODULES, {"paper_operator_view"}) == []
    # live (T8): one test patches the tracker, the next runs it for real -- the patch counts for its own test only;
    # a patch in a fixture counts for the file; a call under `pytest.raises` is meant to fail first
    scoped = '''import pytest
from unittest.mock import patch
from app.modules import paper_operator_view as view

async def test_a(db):
    with patch("app.modules.paper_operator_view.AlpacaPaperClient"):
        await view.build_operator_view(db)

async def test_b(db):
    await view.build_operator_view(db)

async def test_c(db):
    with pytest.raises(ValueError):
        await view.spy_benchmark(db)
'''
    found = runs_secrets_for_real(_files(scoped), MODULES, {"paper_operator_view"})
    assert [f.split(" runs ")[0] for f in found] == ["`tests/test_paper_twelve_modules.py:10`"]
    fixture = "@pytest.fixture(autouse=True)\ndef _no_secrets():\n    with patch('app.modules.runner_secrets.values_for'):\n        yield\n\n" + scoped
    assert runs_secrets_for_real(_files(fixture), MODULES, {"paper_operator_view"}) == []
    decorated = scoped.replace("async def test_b(db):", '@patch("app.modules.paper_alpaca_paper_client.values_for")\nasync def test_b(_m, db):')
    assert runs_secrets_for_real(_files(decorated), MODULES, {"paper_operator_view"}) == []
    # the same call twice: once
    twice = '''from app.modules.paper_edgar_form_4_ingestion import ingest_form_4_filings

async def test_x(db):
    await ingest_form_4_filings(db=db, cik="1")
    await ingest_form_4_filings(db=db, cik="2")
'''
    assert len(runs_secrets_for_real(_files(twice), MODULES, {"paper_operator_view"})) == 1


LIVE_EDGAR = ("E   RuntimeError: Missing operator secret SEC_CONTACT_EMAIL; refusing to run.\n2 failed, 13 passed in 8.0s")
LIVE_ALPACA = ("E   app.modules.paper_alpaca_paper_client.AlpacaSecretsMissingError: Missing Alpaca paper trading secrets: "
               "ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY. Store them on the Machines page -> Secrets.\n2 failed, 7 passed")


def test_the_sandbox_names_the_missing_secret_next_to_the_error():
    cause = et.known_causes(LIVE_EDGAR)
    assert cause.count("Cause the engine recognises") == 1
    assert "patch `values_for` where the module looks it up" in cause
    assert cause.endswith("; this run: `SEC_CONTACT_EMAIL`.")
    assert et.known_causes(LIVE_ALPACA).endswith("; this run: `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`.")
    assert "secret" not in et.known_causes("E   KeyError: 'secret'\n1 failed")
