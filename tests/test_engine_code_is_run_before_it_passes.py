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


def test_a_block_headed_with_another_kind_of_file_is_never_the_steps_module():
    """§17.1838 — live (5b4880c0 T6, 2026-10-26): the output's third block, a self-verdict headed
    `# verifier_response.json` in a python fence, became the step's module -- 480 chars of JSON over the 12,181-char
    batch -- and the sandbox failed it at `name 'true' is not defined`."""
    batch = {"node_key": "T6", "prompt_template": "Python module `paper_batch.py`.", "output_text": (
        "```python\n# app/modules/paper_batch.py\nasync def run():\n    return 1\n```\n\n"
        "```python\n# tests/test_paper_batch.py\ndef test_x():\n    assert True\n```\n\n"
        "```python\n# verifier_response.json\n{\n    \"pass\": true\n}\n```")}
    got = et.files_of(batch)
    assert sorted(got) == ["app/modules/paper_batch.py", "tests/test_paper_batch.py"]
    assert "async def run()" in got["app/modules/paper_batch.py"]
    later = {**batch, "output_text": batch["output_text"].replace("# verifier_response.json\n", "")}
    assert "async def run()" in et.files_of(later)["app/modules/paper_batch.py"]   # unnamed never replaces named
    first = {**batch, "output_text": "```python\n# verdict.json\n{\"pass\": true}\n```\n\n"
                                     "```python\nasync def run():\n    return 1\n```"}
    assert "async def run()" in et.files_of(first)["app/modules/paper_batch.py"]   # the JSON takes no slot


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


# ── §17.1817 — a failed test run reports every failure, from the first ─────────────────────────────────────

def _pytest_out(n: int = 12) -> str:
    """A `pytest -q --tb=short` run with `n` failures, as the sandbox returns it (live: 5b4880c0 T6, 8-14 failing)."""
    body = ["FFFFFFFFFFFF.." + " " * 50 + "[100%]", "=" * 30 + " FAILURES " + "=" * 30]
    for i in range(n):
        body += [f"_____ test_case_{i} _____", f"tests/test_paper_batch.py:{100 + i}: in test_case_{i}",
                 "    await run_daily_batch(db)", "app/modules/paper_batch.py:88: in run_daily_batch",
                 "    qty = equity * FRACTION / price", "    " + "x = 1  # context line " * 12,
                 f"E   ValueError: cause number {i}"]
    body += ["=" * 20 + " short test summary info " + "=" * 20]
    body += [f"FAILED tests/test_paper_batch.py::test_case_{i} - ValueError: cause n..." for i in range(n)]
    body += [f"{n} failed, 2 passed in 3.10s"]
    return "\n".join(body)


def test_the_report_keeps_the_first_failure_and_the_summary():
    out = _pytest_out()
    assert len(out) > 6000                                           # over the limit: something must go
    rep = et._report(out, budget=6000)                               # §17.1869 -- the default is 9000 now
    assert len(rep) <= 6000
    assert rep.startswith("Every failure, one line each") and "test_case_11: E   ValueError" in rep   # §17.1869
    assert "E   ValueError: cause number 0" in rep and "E   ValueError: cause number 1" in rep   # the first ones
    assert "12 failed, 2 passed" in rep and "short test summary info" in rep
    assert "cause number 0" not in et._tail(out)                     # what the old tail kept: not the first


def test_a_short_run_is_reported_whole():
    out = _pytest_out(2)
    assert et._report(out) == "\n".join(ln for ln in out.splitlines() if ln.strip())


@pytest.mark.asyncio
async def test_the_sandbox_runs_short_tracebacks_and_reports_from_the_first_failure():
    run = AsyncMock(return_value=CodeRunResult(ok=False, exit_code=1, stdout=_pytest_out(), stderr=""))
    res = await _check(WITH_TEST, run)
    assert res.verdict == "fail"
    assert "--tb=short" in run.await_args.args[1]
    assert "E   ValueError: cause number 0" in res.reason and "12 failed, 2 passed" in res.reason


# ── §17.1823 — a recognised cause is named next to the error ───────────────────────────────────────────────

LIVE_JSON = ("E   TypeError: descriptor 'encode' for 'str' objects doesn't apply to a 'dict' object\n"
             "E   asyncpg.exceptions.DataError: invalid input for query argument $4: {'ticker': 'SPY'} (descriptor "
             "'encode' for 'str' objects doesn't apply to a 'dict' object)\n1 failed, 2 passed in 1.1s")
LIVE_MOCK = ("    result = await db.execute(\nE   TypeError: 'MagicMock' object can't be awaited\n"
             "1 failed, 7 passed in 1.92s")


def test_a_recognised_failure_gets_its_cause_and_a_miss_gets_none():
    assert "json.dumps(value)" in et.known_causes(LIVE_JSON) and "AsyncMock" not in et.known_causes(LIVE_JSON)
    assert "AsyncMock" in et.known_causes(LIVE_MOCK)
    assert et.known_causes("E   assert 0 == 3\n1 failed") == ""                      # an ordinary failure: nothing
    assert et.known_causes("E   KeyError: 'dict'") == ""


# §17.1825 — the live run (follow-up 5b4880c0 T6, 2026-10-26): two tests, the second awaits the mock in `close`.
LIVE_MOCK_RUN = (
    "________________ test_run_daily_batch_uses_provided_db_session _________________\n"
    "app/modules/paper_day_start_equity_recording.py:59: in record_day_start_equity\n"
    "    result = await db.execute(\n"
    "E   TypeError: 'MagicMock' object can't be awaited\n"
    "________________________ test_own_db_session_is_closed _________________________\n"
    "tests/test_paper_daily_signal_proposal_batch.py:258: in test_own_db_session_is_closed\n"
    "    await run_daily_batch()\n"
    "app/modules/paper_daily_signal_proposal_batch.py:297: in run_daily_batch\n"
    "    await db.close()\n"
    "E   TypeError: 'MagicMock' object can't be awaited\n"
    "2 failed, 7 passed in 1.94s")


def test_the_mock_cause_names_each_call_the_run_awaited_on_a_magicmock():
    cause = et.known_causes(LIVE_MOCK_RUN)
    assert "`db.execute()` (app/modules/paper_day_start_equity_recording.py:59)" in cause
    assert "`db.close()` (app/modules/paper_daily_signal_proposal_batch.py:297)" in cause
    assert "run_daily_batch()" not in cause                     # the test's own call is not the awaited mock
    assert cause.count("Cause the engine recognises") == 1
    # an awaited line before some OTHER error names nothing
    assert "awaited a" not in et.known_causes("    await db.close()\nE   KeyError: 'x'\nE   'MagicMock' object can't be awaited")


@pytest.mark.asyncio
async def test_the_sandbox_reason_carries_the_cause():
    run = AsyncMock(return_value=CodeRunResult(ok=False, exit_code=1, stdout=LIVE_JSON, stderr=""))
    res = await _check(WITH_TEST, run)
    head = res.reason.split("\n\n", 1)[0]
    assert res.verdict == "fail" and "Cause the engine recognises: a Python dict or list was bound" in head


# ── §17.1826 — library frames fold, so every failure of the run reaches the next attempt ─────────────────────

_SA = "/opt/venv/lib/python3.14/site-packages/sqlalchemy/engine/base.py"


def _library_frames(n: int) -> str:
    return "".join(f"{_SA}:{1400 + i}: in _execute_context\n    return self._exec_single_context(\n"
                   f"           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n" for i in range(n))


# The live run's shape (follow-up 5b4880c0 T6, 2026-10-26): a foreign-key error raised through 31 library frames,
# then a second failure the next attempt never saw.
LIVE_FK_RUN = (
    "....FF\n=================================== FAILURES ===================================\n"
    "___________ test_run_daily_batch_calls_dependencies_and_returns_pass ___________\n"
    + _library_frames(15)
    + "asyncpg/protocol/protocol.pyx:205: in bind_execute\n    ???\n"
    "E   asyncpg.exceptions.ForeignKeyViolationError: insert or update on table \"proposal\" violates foreign key "
    "constraint \"proposal_signal_id_fkey\"\nE   DETAIL:  Key (signal_id)=(1) is not present in table \"signal\".\n"
    "The above exception was the direct cause of the following exception:\n"
    "tests/test_paper_daily_signal_proposal_batch.py:116: in test_run_daily_batch_calls_dependencies_and_returns_pass\n"
    "    result = await run_daily_batch(db=db)\n"
    "app/modules/paper_daily_signal_proposal_batch.py:128: in run_daily_batch\n    await write_proposal(\n"
    + _library_frames(16)
    + "E   sqlalchemy.exc.IntegrityError: (ForeignKeyViolationError) proposal_signal_id_fkey\n"
    "___________ test_run_daily_batch_enforces_max_proposals_per_batch ___________\n"
    "tests/test_paper_daily_signal_proposal_batch.py:171: in test_run_daily_batch_enforces_max_proposals_per_batch\n"
    "    assert result[\"proposals\"] == 3\nE   assert 0 == 3\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_paper_daily_signal_proposal_batch.py::test_run_daily_batch_calls_dependencies_and_returns_pass\n"
    "FAILED tests/test_paper_daily_signal_proposal_batch.py::test_run_daily_batch_enforces_max_proposals_per_batch\n"
    "2 failed, 2 passed in 2.84s\n")


def test_library_frames_fold_and_the_second_failure_reaches_the_next_attempt():
    assert len(LIVE_FK_RUN) > 6000                                       # the live run overflowed the budget
    rep = et._report(LIVE_FK_RUN)
    assert "E   assert 0 == 3" in rep                                    # the second failure is there now
    assert "site-packages" not in rep and "protocol.pyx" not in rep
    assert "  … 16 library frames …" in rep and "^^^^" not in rep
    for own in ("tests/test_paper_daily_signal_proposal_batch.py:116: in", "    result = await run_daily_batch(db=db)",
                "app/modules/paper_daily_signal_proposal_batch.py:128: in run_daily_batch", "    await write_proposal(",
                "E   DETAIL:  Key (signal_id)=(1) is not present in table \"signal\".", "2 failed, 2 passed in 2.84s"):
        assert own in rep
