"""§17.1867 — a fake does what the module does with it: the index shows what a function does with each parameter it
reads no kind for, and a test's fake that cannot do it is refused before the sandbox.

Live (follow-up 5b4880c0 T5, 2026-10-29, every attempt): `http_get` fakes returned `"text"` or `(200, "text")` where
`_download_file` calls `.raise_for_status()` and reads `.text`; a client whose `get().json()` returned a list where
`_fetch_house_disclosures` reads `data.get("results")` ('list' object has no attribute 'get'); a patched
`_ensure_client` returned a `MagicMock` whose `.request(…)` the module awaits; the entry point's patched
`AlpacaPaperClient` returned a `MagicMock` whose `.close()` it awaits -- named by the sandbox three attempts running,
with `patch` of an `async def` (an `AsyncMock` by itself) mistaken for the cause."""
from __future__ import annotations

import ast

from app.modules import engine_source as es
from app.modules.engine_contracts import findings_in_tests, wrong_fakes

FINRA = '''# app/modules/paper_finra_data_ingestion.py
import httpx
from app.modules.runner_secrets import values_for

async def _download_file(url: str, *, http_get=None) -> str:
    getter = http_get or _default_http_get
    response = await getter(url)
    response.raise_for_status()
    return response.text

async def _default_http_get(url: str):
    return await httpx.AsyncClient().get(url)

async def ingest_finra_file(*, source: str, http_get=None) -> dict:
    content = await _download_file("u", http_get=http_get)
    return {"n": len(content)}
'''
CONGRESS = '''# app/modules/paper_congressional_trade_ingestion.py
import httpx

async def _fetch_json(client, url, *, params=None):
    response = await client.get(url, params=params, timeout=30.0)
    response.raise_for_status()
    return response.json()

async def _fetch_house(client, *, max_requests: int = 3) -> list:
    results = []
    for _ in range(max_requests):
        data = await _fetch_json(client, "u", params={"page": 1})
        if data is None:
            break
        records = data.get("results") or data.get("data") or []
        results.extend(records)
    return results

async def fetch_disclosures(client=None, *, max_requests: int = 3) -> list:
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()
    try:
        return await _fetch_house(client, max_requests=max_requests)
    finally:
        if own_client and client is not None:
            await client.aclose()
'''
ALPACA = '''# app/modules/paper_alpaca_paper_client.py
import httpx
from app.modules.runner_secrets import values_for

class AlpacaPaperClient:
    async def _ensure_client(self):
        return httpx.AsyncClient()

    async def _request(self, method: str, path: str):
        client = await self._ensure_client()
        creds = await values_for(["ALPACA_API_KEY_ID"])
        response = await client.request(method, path, headers={"k": creds.get("ALPACA_API_KEY_ID")})
        if response.status_code >= 400:
            raise RuntimeError(response.text)
        return response.json()

    async def submit_order(self, symbol: str):
        return await self._request("POST", "/v2/orders")

    async def close(self):
        pass
'''
ENTRY = '''# app/modules/paper_execution_entry_point.py
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def run_execution_entry_point(*, db=None) -> dict:
    client = AlpacaPaperClient()
    try:
        order = await client.submit_order("SPY")
        return {"id": order.id, "status": order.status}
    finally:
        await client.close()
'''
MODULES = {m.split("\n", 1)[0][len("# app/modules/"):-3]: m.split("\n", 1)[1] for m in (FINRA, CONGRESS, ALPACA, ENTRY)}
PATH = "tests/test_paper_twelve_modules.py"


def _shape(code: str, name: str) -> dict:
    tree = ast.parse(code)
    fn = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return es._call_shape(fn, {}, es._local_functions(tree))


def test_the_index_shows_what_a_function_does_with_a_parameter():
    # through `getter = http_get or _default_http_get`, one hop down, and the result's uses
    assert es._shape_signature("ingest_finra_file", _shape(FINRA, "ingest_finra_file")) == \
        "async ingest_finra_file(*, source: str, http_get()->{raise_for_status(), text} = …)"
    # the result of `_fetch_json` is read as a dict by its caller, so `json()` is `: dict`; the `aclose()` under
    # `if own_client …` acts on the module's own client, never on a fake, and is not shown
    assert es._shape_signature("fetch_disclosures", _shape(CONGRESS, "fetch_disclosures")) == \
        "async fetch_disclosures(client{get()->{json(): dict, raise_for_status()}} = …, *, max_requests: int = …)"
    # a parameter with a kind, a value set or keys keeps that rendering; `db` is never described
    sh = _shape(CONGRESS, "fetch_disclosures")
    assert "max_requests" not in sh["uses"] and "client" in sh["uses"]


def _files(test: str) -> dict[str, str]:
    return {PATH: "# " + PATH + "\n" + test}


def test_the_live_fakes_are_refused_with_what_the_module_uses():
    test = '''from unittest.mock import AsyncMock, MagicMock, patch
from app.modules import paper_congressional_trade_ingestion, paper_finra_data_ingestion

async def test_tuple():
    mock_http_get = AsyncMock(return_value=(200, "Settlement Date|Symbol\\n20261014|SPY\\n"))
    await paper_finra_data_ingestion.ingest_finra_file(source="short_interest", http_get=mock_http_get)

async def test_str():
    await paper_finra_data_ingestion.ingest_finra_file(source="short_interest", http_get=AsyncMock(return_value="text"))

async def test_list():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [{"filing_date": "2026-10-01"}]
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    result = await paper_congressional_trade_ingestion.fetch_disclosures(client=mock_client, max_requests=3)
'''
    found = wrong_fakes(_files(test), MODULES, set())
    assert found == [
        "`tests/test_paper_twelve_modules.py:7` hands `ingest_finra_file(…)` `http_get` = a fake whose call returns a tuple "
        "(line 6), and the module calls `.raise_for_status()` on it -- give it what the module uses: "
        "`http_get()->{raise_for_status(), text}`",
        "`tests/test_paper_twelve_modules.py:10` hands `ingest_finra_file(…)` `http_get` = a fake whose call returns a str "
        "(line 10), and the module calls `.raise_for_status()` on it -- give it what the module uses: "
        "`http_get()->{raise_for_status(), text}`",
        "`tests/test_paper_twelve_modules.py:18` hands `fetch_disclosures(…)` `client` = a fake whose `get()` returns a fake "
        "whose `json()` returns a list (line 15), which the module reads as a dict (`.get(…)` / `[\"key\"]`) -- give it "
        "what the module uses: `client{get()->{json(): dict, raise_for_status()}}`",
    ]
    assert findings_in_tests(found) == found                                   # §17.1859: a redraw, not the attempt


def test_a_fake_that_does_what_the_module_does_passes():
    test = '''from unittest.mock import AsyncMock, MagicMock
from app.modules import paper_congressional_trade_ingestion, paper_finra_data_ingestion

async def test_response():
    response = MagicMock(text="a|b", status_code=200)
    await paper_finra_data_ingestion.ingest_finra_file(source="x", http_get=AsyncMock(return_value=response))

async def test_dict():
    mock_response = MagicMock()
    mock_response.json.return_value = {"results": [{"filing_date": "2026-10-01"}]}
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    await paper_congressional_trade_ingestion.fetch_disclosures(client=mock_client)

async def test_magic_client_with_async_get():
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock())
    await paper_congressional_trade_ingestion.fetch_disclosures(client=mock_client)

async def test_unknown():
    await paper_congressional_trade_ingestion.fetch_disclosures(client=make_client())
'''
    assert wrong_fakes(_files(test), MODULES, set()) == []


def test_a_magicmock_the_module_awaits_is_refused_and_a_patched_async_def_is_not():
    test = '''from unittest.mock import AsyncMock, MagicMock, patch
from app.modules import paper_alpaca_paper_client, paper_execution_entry_point

async def test_submit():
    with patch.object(paper_alpaca_paper_client.AlpacaPaperClient, "_ensure_client", new_callable=AsyncMock) as mock_client, \\
            patch.object(paper_alpaca_paper_client, "values_for", new_callable=AsyncMock) as mock_values:
        mock_client.return_value = MagicMock()
        mock_values.return_value = {"ALPACA_API_KEY_ID": "k"}
        await paper_alpaca_paper_client.AlpacaPaperClient().submit_order("SPY")

async def test_entry():
    with patch.object(paper_execution_entry_point, "AlpacaPaperClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        await paper_execution_entry_point.run_execution_entry_point(db=None)

@patch("app.modules.paper_alpaca_paper_client.values_for")
async def test_values_for_patched_bare(mock_values):
    mock_values.return_value = {"ALPACA_API_KEY_ID": "k"}
    await paper_alpaca_paper_client.AlpacaPaperClient().submit_order("SPY")

@patch("app.modules.paper_alpaca_paper_client.AlpacaPaperClient._ensure_client")
async def test_ensure_client_patched_bare(mock_ensure):
    mock_ensure.return_value = AsyncMock()
    await paper_alpaca_paper_client.AlpacaPaperClient().submit_order("SPY")
'''
    found = wrong_fakes(_files(test), MODULES, set())
    assert found == [
        "`tests/test_paper_twelve_modules.py:6` patches `_ensure_client` on `AlpacaPaperClient` with a fake whose call "
        "returns a fake whose `request()` returns a `MagicMock` (line 8), which the module awaits -- a MagicMock cannot be "
        "awaited: make it an `AsyncMock` -- give it what the module uses: "
        "`_ensure_client()->{request()->{json(), status_code, text}}`",
        "`tests/test_paper_twelve_modules.py:13` patches `AlpacaPaperClient` on `paper_execution_entry_point` with a fake "
        "whose call returns a fake whose `submit_order()` returns a `MagicMock` (line 14), which the module awaits -- a "
        "MagicMock cannot be awaited: make it an `AsyncMock` -- give it what the module uses: "
        "`AlpacaPaperClient()->{close(), submit_order()->{id, status}}`",
    ], found
    # `values_for` is `async def` in the engine, `_ensure_client` in the module: `patch(…)` installs an AsyncMock itself
    assert not any("values_for" in f or "test_values_for_patched_bare" in f or ":25`" in f for f in found)
