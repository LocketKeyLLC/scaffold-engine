"""§17.2003 — a GET view that reaches an operator secret still answers when the operator has not stored it, and the
delivery names every secret the job's code reaches -- through the engine's own modules and the route handlers a
module function mounts.

Live (69fe61c4, 2026-11-23): after #1191 was delivered, `/labs/paper/dashboard?span=all` answered 500 --
`get_spy_benchmark` -> `AlpacaPaperClient.get_bars` -> `AlpacaSecretsMissingError` (no Alpaca key stored). The
delivery said "secrets read: -": the handler is defined inside `_make_router()`, and `get_spy_benchmark` is the lab's
earlier module, in the engine and not in the job. Every one of the step's 46 tests stood in for `get_spy_benchmark`,
so the no-key path never ran.

The fixtures are made up in the live shape: an engine client that reads two secrets through `values_for`, an engine
tracker that builds it, and a job's wiring module whose nested dashboard handler calls the tracker."""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import engine_contracts as ec
from app.modules import engine_plan as ep
from app.modules import engine_source as es

CLIENT = '''from app.modules.runner_secrets import values_for


class LabSecretsMissingError(RuntimeError):
    pass


class LabClient:
    async def get_bars(self, symbol):
        creds = await self._creds()
        return [(symbol, creds)]

    async def _creds(self):
        got = await values_for(None, ["LAB_KEY_ID", "LAB_SECRET_KEY"])
        if not got:
            raise LabSecretsMissingError("missing lab secrets")
        return got
'''
TRACKER = '''from app.modules.lab_client import LabClient


async def get_benchmark(start, end):
    client = LabClient()
    return await client.get_bars("SPY")
'''
WIRE_PATH = "app/modules/lab_wire.py"
WIRE = '''from fastapi import APIRouter, FastAPI

from app.modules.lab_tracker import get_benchmark


def _make_router():
    router = APIRouter(prefix="/labs/lab")

    async def _spy():
        return await get_benchmark(None, None)

    @router.get("/dashboard")
    async def dashboard(span: str = "all"):
        return {"span": span, "spy": await _spy()}

    @router.get("/health")
    async def health():
        return {"ok": True}

    return router


def install(app: FastAPI) -> None:
    app.include_router(_make_router())
'''
ENGINE = {"lab_client": CLIENT, "lab_tracker": TRACKER}
TEST_PATH = "tests/test_lab_wire.py"
HEAD = '''from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.lab_wire import install


def _client():
    app = FastAPI()
    install(app)
    return TestClient(app)
'''
#: the live shape: the dashboard requested with the tracker stood in for
STANDS_IN = HEAD + '''

def test_dashboard_renders():
    with patch("app.modules.lab_wire.get_benchmark", AsyncMock(return_value=[])):
        assert _client().get("/labs/lab/dashboard?span=all").status_code == 200
'''
#: the no-key case: the real chain runs into a `values_for` that gives nothing
NO_KEY = HEAD + '''

def test_dashboard_answers_without_the_lab_keys():
    with patch("app.modules.lab_client.values_for", AsyncMock(return_value={})):
        assert _client().get("/labs/lab/dashboard?span=all").status_code == 200
'''


def _files(test: str, wire: str = WIRE) -> dict[str, str]:
    return {WIRE_PATH: wire, TEST_PATH: test}


# ── the tracer ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_handler_inside_a_module_function_is_traced_through_its_nested_helper_into_the_engine():
    reach = {**ENGINE, "lab_wire": WIRE}
    want = {"LAB_KEY_ID": "lab_client", "LAB_SECRET_KEY": "lab_client"}
    assert ec.secrets_read(reach, "lab_wire", "_make_router.dashboard") == want
    assert ec.secrets_read(reach, "lab_wire", "_make_router._spy") == want
    assert ec.secrets_read(reach, "lab_wire", "_make_router.health") == {}
    assert ec.secrets_read(reach, "lab_wire", "_make_router.nothing") == {}
    # the function that mounts the handler does not run it
    assert ec.secrets_read(reach, "lab_wire", "_make_router") == {}
    # stood in for: the chain stops there
    assert ec.secrets_read(reach, "lab_wire", "_make_router.dashboard",
                           patched=frozenset({("lab_tracker", "get_benchmark")})) == {}


def test_the_get_routes_a_module_declares_at_any_depth():
    code = WIRE + '''

router = APIRouter()


@router.get("/top")
async def top():
    return 1


@router.post("/run")
async def run():
    return 2
'''
    assert [(p, h) for p, h, _ln in ec.view_routes(code)] == [
        ("/dashboard", "_make_router.dashboard"), ("/health", "_make_router.health"), ("/top", "top")]
    assert ec.view_routes("def (") == []


def test_the_delivery_names_the_secrets_a_nested_handler_reaches_through_the_engine(monkeypatch):
    monkeypatch.setattr(es, "engine_module_sources", lambda: dict(ENGINE))
    files = {WIRE_PATH: (WIRE, "T8")}
    assert ep.secrets_the_code_reads(files) == ["LAB_KEY_ID", "LAB_SECRET_KEY"]
    # the job's copy wins over the engine's: a client the job rewrote to read nothing
    files["app/modules/lab_client.py"] = (CLIENT.replace('["LAB_KEY_ID", "LAB_SECRET_KEY"]', "[]"), "T9")
    assert ep.secrets_the_code_reads(files) == []
    monkeypatch.setattr(es, "engine_module_sources", lambda: {})
    assert ep.secrets_the_code_reads({WIRE_PATH: (WIRE, "T8")}) == []         # before: the live "secrets read: -"


# ── the gate ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_view_that_reaches_a_secret_and_is_only_tested_with_its_reader_stood_in_for_is_refused():
    found = ec.views_fail_without_secrets(_files(STANDS_IN), ENGINE, {"lab_wire"})
    assert len(found) == 1, found
    f = found[0]
    assert f.startswith("`app/modules/lab_wire.py:13` the view `GET /dashboard` (`_make_router.dashboard`)"), f
    assert "`LAB_KEY_ID`, `LAB_SECRET_KEY`" in f and "through `get_benchmark`" in f and "(read by `lab_client`)" in f
    assert "`app.modules.lab_client.values_for` patched to return `{}`" in f and "answers 200" in f


def test_a_test_that_requests_the_view_with_no_secret_and_the_real_chain_passes():
    assert ec.views_fail_without_secrets(_files(NO_KEY), ENGINE, {"lab_wire"}) == []
    # on `runner_secrets` itself (every reader), and through a fixture the test takes
    fixture = HEAD.replace("from fastapi import FastAPI", "import pytest\nfrom fastapi import FastAPI") + '''

async def _none(*a, **k):
    return {}


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.setattr("app.modules.runner_secrets.values_for", _none)


def test_dashboard_without_keys(no_keys):
    assert _client().get("/labs/lab/dashboard").status_code == 200
'''
    assert ec.views_fail_without_secrets(_files(fixture), ENGINE, {"lab_wire"}) == []


@pytest.mark.parametrize("test", [
    # the secrets given: the no-key path never runs
    NO_KEY.replace("return_value={}", 'return_value={"LAB_KEY_ID": "k", "LAB_SECRET_KEY": "s"}'),
    # no secret, but the tracker stood in for as well: the client never runs
    NO_KEY.replace('    with patch("app.modules.lab_client.values_for"',
                   '    with patch("app.modules.lab_wire.get_benchmark", AsyncMock(return_value=[])), '
                   'patch("app.modules.lab_client.values_for"'),
    # no secret, but another route requested
    NO_KEY.replace("/labs/lab/dashboard?span=all", "/labs/lab/health"),
    # no secret on a module that does not read it
    NO_KEY.replace("app.modules.lab_client.values_for", "app.modules.lab_tracker.values_for"),
])
def test_a_test_that_does_not_run_the_view_without_its_secret_does_not_count(test):
    assert len(ec.views_fail_without_secrets(_files(test), ENGINE, {"lab_wire"})) == 1


def test_a_view_that_reaches_no_secret_or_a_module_the_step_does_not_write_is_not_held():
    plain = WIRE.replace("await _spy()", "[]")
    assert ec.views_fail_without_secrets(_files(STANDS_IN, plain), ENGINE, {"lab_wire"}) == []
    assert ec.views_fail_without_secrets(_files(STANDS_IN), ENGINE, {"lab_other"}) == []


def test_a_stand_in_that_gives_no_secret_in_a_test_that_requests_a_view_is_not_a_wrong_secret_mock():
    """§17.1863 refused a `values_for` stand-in missing a name the run reads; one that gives nothing, in a test that
    requests a view, is the not-stored case on purpose -- the §17.2003 test. Anywhere else it is still refused
    (§17.1875: a run handed nothing, not expecting the raise)."""
    with_view = NO_KEY.replace("from app.modules.lab_wire import install",
                               "from app.modules.lab_tracker import get_benchmark\nfrom app.modules.lab_wire import install")
    with_view = with_view.replace("        assert _client()", "        await get_benchmark(None, None)\n        assert _client()")
    with_view = with_view.replace("def test_dashboard_answers", "async def test_dashboard_answers")
    assert ec.wrong_secret_mocks(_files(with_view), ENGINE, {"lab_wire"}) == []
    run = '''from unittest.mock import AsyncMock, patch

from app.modules.lab_tracker import get_benchmark


async def test_x():
    with patch("app.modules.lab_client.values_for", AsyncMock(return_value={})):
        await get_benchmark(None, None)
'''
    assert len(ec.wrong_secret_mocks({TEST_PATH: run}, ENGINE, set())) == 1        # no view requested: refused
    wrong = with_view.replace("return_value={}", 'return_value={"LAB_KEY_ID": "k"}')
    assert len(ec.wrong_secret_mocks(_files(wrong), ENGINE, {"lab_wire"})) == 1   # a wrong name is still refused


def test_the_wiring_note_tells_the_step_before_the_gate_refuses():
    note = ep._INSTALL_NOTE.format(mod="lab_wire")
    assert "still answers when the operator has not stored that secret" in note
    assert "`values_for` patched to return `{}`" in note and "(§17.2003)" in note
    assert ep._INSTALL_NOTE_RE.search(note) is not None and ep._INSTALL_NOTE_RE.sub("", note).strip() == ""


# ── the gate in the step's checks, on a real database ──────────────────────────────────────────────────────────

def _out(*files: tuple[str, str]) -> str:
    return "The dashboard.\n\n" + "".join(f"```python\n# {p}\n{c}```\n\n" for p, c in files)


@pytest.mark.integration
@pytest.mark.timeout(300)
async def test_engine_findings_refuses_the_live_shape_and_passes_the_no_key_test(tmp_path, monkeypatch):
    from sqlalchemy import text

    from app.database import async_session
    es.module_index()
    (tmp_path / "modules").mkdir()
    for m, c in ENGINE.items():
        (tmp_path / "modules" / f"{m}.py").write_text(c)
    monkeypatch.setattr(es, "ENGINE_PKG", tmp_path)
    jid = str(uuid.uuid4())
    brief = {"title": "dash", "description": "Deliver the dashboard into the engine through the lab's delivery flow."}
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                              "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(brief)})
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, tool, execution_order) "
            "VALUES (:j, 'T8', 'Add dashboard route', 'running', 'Python module `lab_wire.py`', 'CodeGen', 0)"),
            {"j": jid})
        await db.commit()
    try:
        async with async_session() as db:
            live = await es.engine_findings(db, jid, "T8", _out((WIRE_PATH, WIRE), (TEST_PATH, STANDS_IN)), [])
            fixed = await es.engine_findings(db, jid, "T8", _out((WIRE_PATH, WIRE), (TEST_PATH, NO_KEY)), [])
        assert sum("the view `GET /dashboard`" in f for f in live) == 1, live
        assert not any("the view `GET /dashboard`" in f for f in fixed), fixed
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :a"), {"a": jid})
            await db.commit()


# ── the plan pass: a finished module's view gets the step that fixes it ────────────────────────────────────────

def _plan() -> list[dict]:
    return [
        {"id": "T8", "name": "Add dashboard route", "tool": "CodeGen", "status": "done", "depends_on": []},
        {"id": "T12L1", "name": "Fix test_lab_wire", "tool": "CodeGen", "status": "done", "depends_on": [],
         "tool_config": {"fixes_tests_of": "lab_wire"}},
        {"id": "T12", "name": "Is this job's code merged into the engine and running?", "tool": "LLM",
         "type": "decision", "status": "pending", "depends_on": ["T8", "T12L1"]},
        {"id": "T13", "name": "Verify dashboard renders", "tool": "LLM", "status": "pending", "depends_on": ["T12"]},
        {"id": "T11", "name": "Verify existing routes unchanged", "tool": "LLM", "status": "done", "depends_on": ["T8"]},
    ]


def test_a_finished_view_that_fails_without_its_secret_gets_one_fix_step_after_its_last_writers():
    tasks = _plan()
    files = {WIRE_PATH: (WIRE, "T8"), TEST_PATH: (STANDS_IN, "T12L1")}
    changes = ep.views_answer_without_secrets(tasks, files, ENGINE)
    assert changes == ["views_answer_without_secrets: NOKEY edits `lab_wire.py` so `GET /dashboard` answers with no "
                       "key stored"]
    step = tasks[-1]
    assert (step["id"], step["tool"], step["depends_on"]) == ("NOKEY", "CodeGen", ["T8", "T12L1"])
    assert step["name"] == "Make GET /dashboard answer without the secrets it reaches"
    assert step["tool_config"] == {"fixes_module": "lab_wire", "answers_without_secrets": "lab_wire"}
    assert "Edit `lab_wire.py` in place" in step["notes"] and "the view `GET /dashboard`" in step["notes"]
    assert "`values_for` patched to return `{}`" in step["notes"]
    deps = {t["id"]: t["depends_on"] for t in tasks}
    assert deps["T12"] == ["T8", "T12L1", "NOKEY"]                  # the delivery waits on it
    assert deps["T13"] == ["T12"] and deps["T11"] == ["T8"]         # behind the delivery already; a finished check
    assert ep.views_answer_without_secrets(tasks, files, ENGINE) == []          # once
    assert len(tasks) == 6


def test_the_pass_leaves_a_view_that_answers_without_its_secret_and_a_job_with_no_view_alone():
    tasks = _plan()
    assert ep.views_answer_without_secrets(tasks, {WIRE_PATH: (WIRE, "T8"), TEST_PATH: (NO_KEY, "T12L1")}, ENGINE) == []
    assert ep.views_answer_without_secrets(tasks, {"app/modules/lab_tracker.py": (TRACKER, "T8")}, ENGINE) == []
    assert len(tasks) == 5
