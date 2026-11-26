"""§17.2028 — a method called on an instance of a job class is checked against its signature when the class has no
`__init__` too.

§17.1889 checked `client.submit_order(…)` once `client = AlpacaPaperClient(…)` was bound, but `_instance_of` took the
class from the import map, which listed what is CALLED against a shape: functions and constructors. A class with no
`__init__` (and not a data class) has no constructor shape, so `client = Client()` was never an instance and every
method call on it went unchecked (parked since §17.1889: "`_instance_of` reads only classes with an `__init__`").
"""
from __future__ import annotations

from app.modules import engine_source as es

# a client with no `__init__`: its methods read the module's own settings
CLIENT = '''# app/modules/paper_quote_client.py
QUOTE_URL = "https://quotes.invalid/v2"

class QuoteClient:
    async def latest_quote(self, symbol: str, *, feed: str) -> dict:
        return {"symbol": symbol, "feed": feed}

    def spread(self, bid, ask):
        return ask - bid

class SizedClient:
    def __init__(self, size: int):
        self.size = size

    def take(self, n: int) -> int:
        return min(n, self.size)
'''


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


def _findings(test: str) -> list[str]:
    defs = es.sibling_definitions([_row("T3", CLIENT)])
    return [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs)]


def test_a_method_of_a_class_with_no_init_is_checked_through_every_import_shape():
    test = '''# tests/test_paper_quote_client.py
from app.modules import paper_quote_client
from app.modules.paper_quote_client import QuoteClient, SizedClient

async def test_quote_file_import():
    client = QuoteClient()
    await client.latest_quote("SPY")

def test_quote_module_alias():
    client = paper_quote_client.QuoteClient()
    client.spread(1)

async def test_quote_local_import():
    from app.modules.paper_quote_client import QuoteClient as QC
    c = QC()
    await c.latest_quote(feed="iex")

def test_sized_still_read():
    s = SizedClient(3)
    s.take()
'''
    got = sorted(_findings(test), key=lambda f: int(f.split(":", 1)[1].split("`", 1)[0]))
    assert got == [
        "`tests/test_paper_quote_client.py:7` calls `QuoteClient.latest_quote(…)` is missing required argument(s) "
        "`feed`",
        "`tests/test_paper_quote_client.py:11` calls `QuoteClient.spread(…)` is missing required argument(s) `ask`",
        "`tests/test_paper_quote_client.py:16` calls `QuoteClient.latest_quote(…)` is missing required argument(s) `symbol`",
        "`tests/test_paper_quote_client.py:20` calls `SizedClient.take(…)` is missing required argument(s) `n`",
    ], got


def test_a_correct_use_of_a_class_with_no_init_is_left_alone():
    test = '''# tests/test_paper_quote_client.py
from app.modules.paper_quote_client import QuoteClient

async def test_quote():
    client = QuoteClient()
    got = await client.latest_quote("SPY", feed="iex")
    assert got["feed"] == "iex"
    assert client.spread(1, 2) == 1
'''
    assert _findings(test) == []
