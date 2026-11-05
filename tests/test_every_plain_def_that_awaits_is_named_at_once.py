"""§17.1927 — a test file with many plain `def` tests that `await` is refused for every one of them at once, not the
first the compiler names. Live (follow-up 5b4880c0 API_ALPACA_AUDITF1, 2026-11-05 09:17 UTC): 27 tests written `def`
with `await` inside; each redraw mended the one named, five before the asks ran out, and the step's last attempt was
refused for the 22 never named."""
from __future__ import annotations

from app.modules.engine_source import syntax_only_in_tests
from app.modules.engine_source import test_syntax_findings as syntax_findings

PATH = "tests/test_paper_alpaca_paper_client.py"


def _out(body: str) -> str:
    return f"```python\n# {PATH}\nimport pytest\n\n{body}```\n"


SYNC_AWAITS = '''
def test_a(client):
    assert await client.get() == 1


@pytest.mark.asyncio
async def test_b(client):
    async def inner():
        return 2
    assert await inner() == 2


def test_c(client):
    async def helper():
        return await client.get()
    assert helper


def test_d(client):
    async with client as c:
        assert c
'''


def test_every_plain_def_that_awaits_is_named_and_a_nested_async_def_is_its_own():
    out = _out(SYNC_AWAITS)
    found = syntax_findings(out)
    assert [f.split("`")[3] for f in found] == ["def test_a", "def test_d"]
    assert found[0].startswith(f"`{PATH}:5` does not compile: 'await' outside async function")
    assert "make it `async def` with `@pytest.mark.asyncio`" in found[0]
    assert syntax_only_in_tests(out)


def test_another_syntax_error_is_named_as_the_compiler_names_it():
    found = syntax_findings(_out("async def def test_x():\n    pass\n"))
    assert len(found) == 1 and found[0].startswith(f"`{PATH}:4` does not compile:") and "fix that line" in found[0]
