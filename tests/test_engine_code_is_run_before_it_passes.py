"""§17.1765 — a code step written for the engine is run in the engine-test sandbox before it passes.

Live (job 2772acae): nothing ever ran the lab's code. Measured against the real outputs in the sandbox (2026-10-23):
19 modules imported with their siblings present, T12 failed on `cannot import name 'get_open_orders' from
'app.modules.alpaca_stream'`, and a test of T11's `check_allowlist("ZZZZ")` failed -- the allow-list admits any
ticker. These tests pin the client: which files a step writes, what is run, and how each sandbox answer lands.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.sandbox import engine_tests as et
from app.sandbox.client import CodeRunResult

T10 = {"node_key": "T10", "status": "done", "prompt_template": "Python module `alpaca_stream.py` with `start_stream`.",
       "output_text": "```python\nasync def start_stream(cb):\n    ...\n```"}
T16 = {"node_key": "T16", "status": "done", "prompt_template": "",
       "output_text": "```python\n# app/modules/audit_writer.py\nasync def append_audit_event(**kw):\n    ...\n```"}
T99 = {"node_key": "T99", "status": "failed", "prompt_template": "Python module `broken.py`",
       "output_text": "```python\nraise SystemExit\n```"}
ME = {"node_key": "T12", "prompt_template": "Python module `paper_kill_switch.py`"}
MODULE = "```python\nfrom app.modules.alpaca_stream import start_stream\n```"
WITH_TEST = MODULE + "\n\n```python\n# tests/test_paper_kill_switch.py\ndef test_x():\n    assert True\n```"


def test_each_block_lands_at_its_header_or_the_steps_own_module():
    got = {k: v.strip() for k, v in et.files_of({**ME, "output_text": WITH_TEST}).items()}
    assert got == {
        "app/modules/paper_kill_switch.py": "from app.modules.alpaca_stream import start_stream",
        "tests/test_paper_kill_switch.py": "# tests/test_paper_kill_switch.py\ndef test_x():\n    assert True"}
    assert list(et.files_of(T16)) == ["app/modules/audit_writer.py"]
    bare_test = {"prompt_template": "Python module `test_integration.py`", "output_text": "```python\ndef test_a(): pass\n```"}
    assert list(et.files_of(bare_test)) == ["tests/test_integration.py"]
    migration = {"output_text": "```python\n# alembic/versions/0010_lab.py\nrevision = '0010_lab'\n```"}
    assert list(et.files_of(migration)) == ["alembic/versions/0010_lab.py"]    # §17.1783: applied to the scratch db


def _ok(stdout="1 passed"):
    return CodeRunResult(ok=True, exit_code=0, stdout=stdout)


async def _check(output, run, *, digest="d1", here="d1"):
    with patch.object(et.settings, "engine_test_check_enabled", True), \
         patch.object(et.settings, "engine_test_url", "http://sandbox:8020"), \
         patch.object(et, "_sandbox_engine", new=AsyncMock(return_value=digest)), \
         patch.object(et, "_local_digest", return_value=here), \
         patch.object(et, "run_code", new=run):
        return await et.check(output, ME, [T10, T16, T99, {**ME, "output_text": "old draft", "status": "failed"}])


@pytest.mark.asyncio
async def test_a_step_with_tests_runs_them_with_its_finished_siblings_present():
    run = AsyncMock(return_value=_ok())
    res = await _check(WITH_TEST, run)
    assert res.verdict == "pass" and res.ran == ["tests/test_paper_kill_switch.py"]
    files, command = run.await_args.args
    assert {"app/modules/alpaca_stream.py", "app/modules/audit_writer.py", "app/modules/paper_kill_switch.py",
            "tests/test_paper_kill_switch.py", "tests/__init__.py"} <= set(files)
    assert "app/modules/broken.py" not in files                   # a failed sibling is not part of the engine
    assert command[:3] == ["python", "-m", "pytest"] and command[-1] == "tests/test_paper_kill_switch.py"
    assert run.await_args.kwargs["base_url"] == "http://sandbox:8020"


@pytest.mark.asyncio
async def test_a_module_without_tests_is_imported():
    run = AsyncMock(return_value=_ok("imported 1"))
    res = await _check(MODULE, run)
    assert res.verdict == "pass"
    assert "app.modules.paper_kill_switch" in run.await_args.args[1][-1]


@pytest.mark.asyncio
async def test_a_failure_is_the_reason_the_next_attempt_reads():
    run = AsyncMock(return_value=CodeRunResult(ok=False, exit_code=1, stdout="E   assert not True\n1 failed"))
    res = await _check(WITH_TEST, run)
    assert res.verdict == "fail" and "§17.1765" in res.reason and "assert not True" in res.reason
    assert "app/modules/alpaca_stream.py" in res.reason and "mock `async_session`" in res.reason


@pytest.mark.asyncio
async def test_no_tests_collected_and_a_timeout_say_what_happened():
    res = await _check(WITH_TEST, AsyncMock(return_value=CodeRunResult(ok=False, exit_code=5)))
    assert res.verdict == "fail" and "collected no tests" in res.reason
    res = await _check(WITH_TEST, AsyncMock(return_value=CodeRunResult(ok=False, exit_code=-1, timed_out=True)))
    assert res.verdict == "fail" and "timed out" in res.reason


@pytest.mark.asyncio
async def test_off_unreachable_or_another_engine_is_a_skip_never_a_pass():
    run = AsyncMock(return_value=_ok())
    assert (await _check(WITH_TEST, run, digest=None)).verdict == "skip"
    assert (await _check(WITH_TEST, run, digest="old", here="new")).verdict == "skip"
    run.assert_not_awaited()
    with patch.object(et.settings, "engine_test_check_enabled", False):
        assert (await et.check(WITH_TEST, ME, [])).verdict == "skip"
    res = await _check(WITH_TEST, AsyncMock(return_value=CodeRunResult(ok=False, error="call failed")))
    assert res.verdict == "skip"


def test_the_digest_matches_the_sandboxs_own():
    """The orchestrator and the sandbox must compute the same digest of the same tree."""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("enginetest_server", root / "docker" / "enginetest" / "server.py")
    if spec is None or spec.loader is None or not (root / "docker" / "enginetest" / "server.py").exists():
        pytest.skip("docker/ is not in this image")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app_dir = root / "app"
    assert mod.engine_digest(app_dir) == et._digest_of(str(app_dir))
    assert mod.safe_relpath("app/modules/x.py") and mod.safe_relpath("tests/t.py")
    assert mod.safe_relpath("../etc/passwd") is None and mod.safe_relpath("/etc/x") is None
    assert mod.safe_relpath("docker/x.py") is None


# ── the production path ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_executor_fails_a_step_whose_sandbox_run_fails():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    failing = AsyncMock(return_value=SimpleNamespace(verdict="fail", reason="§17.1765 engine-test sandbox: boom",
                                                     ran=["app/modules/news_client.py"]))
    with patch("app.sandbox.engine_tests.check", new=failing), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        _, reasons, verify = await _run(GROUNDED, LIVE_BRIEF)
    assert any("§17.1765" in r for r in reasons), reasons
    failing.assert_awaited()
    verify.assert_not_awaited()                                    # refused before the LLM verifier is spent


@pytest.mark.asyncio
async def test_the_executor_does_not_run_another_jobs_code():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, _run
    spy = AsyncMock()
    with patch("app.sandbox.engine_tests.check", new=spy):
        await _run(GROUNDED, {"description": "Write a news CLI.", "goals": ["A CLI."]})
    spy.assert_not_awaited()


# ── §17.1783 the sandbox's scratch database ─────────────────────────────────

T3_REVISION = {"node_key": "T3", "status": "done", "prompt_template": "",
               "output_text": "```python\n# alembic/versions/0010_paper_audit_trail.py\nrevision = '0010_paper_audit_trail'\n```"}


def test_a_jobs_revision_is_carried_into_the_run():
    """Live: `0010_paper_audit_trail.py` starts with digits; the header reader required a letter and dropped it, so the
    scratch database stayed at the engine's 0009 and every table of the job was missing."""
    assert list(et.files_of(T3_REVISION)) == ["alembic/versions/0010_paper_audit_trail.py"]


@pytest.mark.asyncio
async def test_the_run_asks_for_a_database_and_never_shadows_alembic():
    run = AsyncMock(return_value=CodeRunResult(ok=True, exit_code=0, stdout="1 passed", database="ok"))
    with patch.object(et.settings, "engine_test_check_enabled", True), \
         patch.object(et.settings, "engine_test_url", "http://sandbox:8020"), \
         patch.object(et, "_sandbox_engine", new=AsyncMock(return_value="d")), \
         patch.object(et, "_local_digest", return_value="d"), \
         patch.object(et, "run_code", new=run):
        res = await et.check(WITH_TEST, ME, [T10, T3_REVISION])
    files = run.await_args.args[0]
    assert res.verdict == "pass" and run.await_args.kwargs["database"] is True
    assert "alembic/versions/0010_paper_audit_trail.py" in files
    assert not any(p.startswith("alembic/") and p.endswith("__init__.py") for p in files)   # would shadow alembic


@pytest.mark.asyncio
async def test_a_failure_with_the_database_says_to_test_sql_against_it():
    run = AsyncMock(return_value=CodeRunResult(ok=False, exit_code=1, stdout="E assert 3 == 2", database="ok"))
    with patch.object(et.settings, "engine_test_check_enabled", True), \
         patch.object(et.settings, "engine_test_url", "http://sandbox:8020"), \
         patch.object(et, "_sandbox_engine", new=AsyncMock(return_value="d")), \
         patch.object(et, "_local_digest", return_value="d"), \
         patch.object(et, "run_code", new=run):
        res = await et.check(WITH_TEST, ME, [T10])
    assert "fresh scratch database" in res.reason and "fake session that ignores the query" in res.reason


# ── §17.1810 the verdict is kept on the step ─────────────────────────────────
# Live (lab f00ea97f, 2026-10-24): asked "were the trading modules tested?", the only record of each sandbox verdict
# was a log line, gone with the orchestrator's restart; and "done" said nothing of whether a step's own tests ran or
# only an import of its module did (12 of 23 had no tests at all).

def test_the_record_says_whether_tests_ran_or_only_an_import():
    assert et.record(et.EngineTestResult("pass", "tests passed (tests/test_x.py)", ["tests/test_x.py"])) == {
        "verdict": "pass", "kind": "tests", "ran": ["tests/test_x.py"], "reason": "tests passed (tests/test_x.py)"}
    assert et.record(et.EngineTestResult("pass", "imports cleanly", ["app/modules/x.py", "app/modules/y.py"]))["kind"] \
        == "import"
    assert et.record(et.EngineTestResult("skip", "engine-test sandbox off", []))["kind"] == "none"
    assert len(et.record(et.EngineTestResult("fail", "x" * 9000, ["tests/test_x.py"]))["reason"]) == 500


def _evidence_written(db) -> list[dict]:
    import json
    return [json.loads(c.args[1]["ev"]) for c in db.execute.await_args_list
            if "SET evidence" in str(c.args[0]) and len(c.args) > 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,ran,kind", [
    ("pass", ["tests/test_news_client.py"], "tests"),
    ("pass", ["app/modules/news_client.py"], "import"),
    ("fail", ["tests/test_news_client.py"], "tests"),
])
async def test_the_executor_keeps_the_verdict_on_the_step(verdict, ran, kind):
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    answer = AsyncMock(return_value=SimpleNamespace(verdict=verdict, reason=f"§17.1765 {verdict}", ran=ran))
    with patch("app.sandbox.engine_tests.check", new=answer), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    kept = [e["engine_test"] for e in _evidence_written(captured["db"]) if "engine_test" in e]
    assert kept and kept[-1]["verdict"] == verdict and kept[-1]["kind"] == kind and kept[-1]["ran"] == ran, \
        (kept, captured.get("raised"))


@pytest.mark.asyncio
async def test_another_jobs_code_keeps_no_sandbox_verdict():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, _run
    captured, _, _ = await _run(GROUNDED, {"description": "Write a news CLI.", "goals": ["A CLI."]})
    assert not [e for e in _evidence_written(captured["db"]) if "engine_test" in e]


@pytest.mark.asyncio
async def test_the_verdict_is_kept_when_the_evidence_check_itself_errors():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    answer = AsyncMock(return_value=SimpleNamespace(verdict="pass", reason="tests passed", ran=["tests/test_n.py"]))
    with patch("app.sandbox.engine_tests.check", new=answer), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])), \
         patch("app.modules.execution_agent.verify_node_output", new=AsyncMock(side_effect=RuntimeError("down"))):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    kept = [e for e in _evidence_written(captured["db"]) if "engine_test" in e]
    assert kept and kept[-1]["checked"] is False and kept[-1]["engine_test"]["kind"] == "tests", \
        (kept, captured.get("raised"))


def test_the_run_tab_says_tested_or_only_loaded_before_the_evidence_gate():
    import pathlib
    import re
    ui = pathlib.Path("app/ui/static/views/theater.js").read_text()
    body = re.search(r"function evidenceLines\(ev\) \{(.*?)\n  \}\n", ui, re.S).group(1)
    et_at, gate_at = body.index("ev.engine_test"), body.index("if (!ev.checked) return out;")
    assert et_at < gate_at                       # an unchecked report still shows what the sandbox ran
    for phrase in ("✓ Tested", "⚠️ Not tested", "❌ The engine-test sandbox failed", "Not run in the engine-test sandbox"):
        assert phrase in body
