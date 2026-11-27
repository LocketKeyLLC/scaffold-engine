"""§17.2053 — an integration test must ask an engine that reads the database it wrote.

The test process's database is the TEST database: tests/conftest.py refuses any
``DATABASE_URL`` whose name does not end in ``_test`` (§17.1108). The running
orchestrator (``scaffold-orchestrator:8000`` / ``localhost:8000``) reads
PRODUCTION. So a tests/integration module that seeds rows through
``async_session`` (or the conftest fixtures built on it) and then calls that
engine over HTTP can never find its rows: on main 6e4aa3b3 six files did exactly
this and produced 14 ``assert 404 == 200`` failures (``spec <uuid> not found``,
``job <uuid> not found`` ...), unseen because CI's Tier 2 is gated off and the
Postgres-only lane skipped them as ``needs_orchestrator``.

The fix is to ask an engine on the same database: in-process through
``engine_client`` (tests/integration/conftest.py) or an engine subprocess the
test starts itself on its own port (test_runner_loop_live.py's pattern). This
gate fails any module that pairs a test-DB write with the live engine, using
``_live_write_guard.targets_live_engine`` — the suite's one definition of "the
live engine" — so the two cannot drift apart.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests._live_write_guard import targets_live_engine

pytestmark = pytest.mark.smoke

INTEGRATION_DIR = Path(__file__).resolve().parent / "integration"

#: What says "this module reads/writes the test process's database": the session
#: factory itself, or a tests/integration/conftest.py fixture built on it.
_TEST_DB_TOKENS = ("async_session", "insert_job", "db_session", "track_job", "tracked_jobs")

#: A URL fragment inside any string literal (including f-string parts).
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"'{}]+", re.IGNORECASE)


def _string_fragments(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _live_engine_urls(source: str) -> list[str]:
    """Every literal URL in ``source`` that addresses the running orchestrator.

    A URL whose port is filled in by formatting (``f"http://127.0.0.1:{PORT}"``
    leaves the fragment ``http://127.0.0.1:``) is an engine the test starts for
    itself and is not judged.
    """
    hits = []
    for frag in _string_fragments(ast.parse(source)):
        for url in _URL.findall(frag):
            if url.endswith(":"):
                continue
            if targets_live_engine(url):
                hits.append(url)
    return hits


def _touches_test_db(source: str) -> bool:
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {a.arg for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              for a in n.args.args}
    names |= {alias.asname or alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
              for alias in n.names}
    return any(t in names for t in _TEST_DB_TOKENS)


def _marks_needs_orchestrator(source: str) -> bool:
    tree = ast.parse(source)
    return any(isinstance(n, ast.Attribute) and n.attr == "needs_orchestrator" for n in ast.walk(tree))


def offence(source: str) -> str | None:
    """Why this module asks the wrong engine for its rows, or None."""
    if not _touches_test_db(source):
        return None
    urls = _live_engine_urls(source)
    if urls:
        return f"uses the test DB and calls the live engine at {urls[0]}"
    if _marks_needs_orchestrator(source):
        return "uses the test DB and is marked needs_orchestrator (calls the live engine)"
    return None


def offenders(root: Path) -> list[str]:
    out = []
    for path in sorted(root.glob("test_*.py")):
        why = offence(path.read_text(encoding="utf-8"))
        if why:
            out.append(f"{path.name}: {why}")
    return out


def test_no_integration_test_asks_the_live_engine_for_rows_it_wrote_to_the_test_db():
    assert INTEGRATION_DIR.is_dir(), INTEGRATION_DIR
    found = offenders(INTEGRATION_DIR)
    assert not found, (
        "these tests/integration modules seed the TEST database (§17.1108) and then ask "
        "the running orchestrator, which reads PRODUCTION — every lookup 404s. Call the "
        "app in-process with the `engine_client` fixture, or start an engine of your own "
        "on another port (§17.2053):\n  " + "\n  ".join(found)
    )


# -- the gate must bite (a hit) and must not fire on the fixed shapes (a miss) ------

_MAIN_SHAPE = '''
import httpx
from app.database import async_session
ORCHESTRATOR_URL = "http://scaffold-orchestrator:8000"

async def test_confirm(inserted_spec):
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"{ORCHESTRATOR_URL}/specs/x/confirm")
'''

_LOCALHOST_SHAPE = '''
from app.database import async_session
BASE_URL = "http://localhost:8000"
'''

_MARKER_ONLY_SHAPE = '''
import pytest
pytestmark = pytest.mark.needs_orchestrator

async def test_x(insert_job):
    pass
'''

_FIXED_SHAPE = '''
from app.database import async_session

async def test_confirm(engine_client, inserted_spec):
    async with engine_client(timeout=10.0) as client:
        await client.post("/specs/x/confirm")
'''

_OWN_ENGINE_SHAPE = '''
from app.database import async_session
ENGINE_PORT = 8001
BASE = f"http://127.0.0.1:{ENGINE_PORT}"
'''

_NO_DB_SHAPE = '''
import httpx
async def test_health():
    httpx.get("http://scaffold-orchestrator:8000/health")
'''


@pytest.mark.parametrize("source", [_MAIN_SHAPE, _LOCALHOST_SHAPE, _MARKER_ONLY_SHAPE])
def test_the_gate_flags_a_test_db_write_paired_with_the_live_engine(source):
    assert offence(source)


@pytest.mark.parametrize("source", [_FIXED_SHAPE, _OWN_ENGINE_SHAPE, _NO_DB_SHAPE])
def test_the_gate_passes_an_engine_on_the_same_db_or_no_db_at_all(source):
    assert offence(source) is None
