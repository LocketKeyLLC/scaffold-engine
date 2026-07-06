"""§17.434 — unit tests for the sandbox exec-smoke classifier.

Offline: run_code is mocked. Verifies the pass / skip / fail classification,
especially that unresolved sibling imports and a disabled/unreachable sandbox
are SKIP (never a false FAIL), while a real runtime error is FAIL.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.sandbox import codegen_check
from app.sandbox.client import CodeRunResult

pytestmark = pytest.mark.smoke

_PY = "```python\nx = 1\n```"


def _patch_run(monkeypatch, result: CodeRunResult):
    monkeypatch.setattr(codegen_check, "run_code", AsyncMock(return_value=result))


@pytest.mark.asyncio
async def test_no_python_block_is_skip(monkeypatch):
    boom = AsyncMock(side_effect=AssertionError("run_code must not be called"))
    monkeypatch.setattr(codegen_check, "run_code", boom)
    r = await codegen_check.codegen_exec_smoke("just prose, no code")
    assert r.verdict == "skip"


@pytest.mark.asyncio
async def test_sandbox_unavailable_is_skip(monkeypatch):
    _patch_run(monkeypatch, CodeRunResult(ok=False, error="coderunner disabled"))
    r = await codegen_check.codegen_exec_smoke(_PY)
    assert r.verdict == "skip" and "unavailable" in r.reason


@pytest.mark.asyncio
async def test_clean_execution_is_pass(monkeypatch):
    _patch_run(monkeypatch, CodeRunResult(ok=True, exit_code=0, stdout=""))
    r = await codegen_check.codegen_exec_smoke(_PY)
    assert r.verdict == "pass"


@pytest.mark.asyncio
async def test_timeout_is_skip_not_fail(monkeypatch):
    _patch_run(monkeypatch, CodeRunResult(ok=False, exit_code=-9, timed_out=True))
    r = await codegen_check.codegen_exec_smoke(_PY)
    assert r.verdict == "skip"


@pytest.mark.asyncio
async def test_unresolved_import_is_skip(monkeypatch):
    _patch_run(monkeypatch, CodeRunResult(
        ok=False, exit_code=1,
        stderr="Traceback...\nModuleNotFoundError: No module named 'table'",
    ))
    r = await codegen_check.codegen_exec_smoke(
        "```python\nfrom table import render_table\n```"
    )
    assert r.verdict == "skip" and "sibling" in r.reason


@pytest.mark.asyncio
async def test_runtime_error_is_fail(monkeypatch):
    _patch_run(monkeypatch, CodeRunResult(
        ok=False, exit_code=1,
        stderr="Traceback (most recent call last):\n  ...\nNameError: name 'undefined' is not defined",
    ))
    r = await codegen_check.codegen_exec_smoke("```python\nprint(undefined)\n```")
    assert r.verdict == "fail"
    assert "§17.434" in r.reason and "NameError" in r.reason


# ── §17.497 — required-arg CLI invoked with no argv is skip, not fail ────────


@pytest.mark.asyncio
async def test_cli_required_args_is_skip(monkeypatch):
    """A correct CLI run with no argv exits 2 via argparse — that's 'can't run
    standalone without args', not a code defect (mirrors the import-miss skip)."""
    _patch_run(monkeypatch, CodeRunResult(
        ok=False, exit_code=2,
        stderr=("usage: solution.py [-h] --prefix PREFIX --ext EXT\n"
                "solution.py: error: the following arguments are required: --prefix, --ext"),
    ))
    r = await codegen_check.codegen_exec_smoke(
        "```python\nimport argparse\np=argparse.ArgumentParser()\n"
        "p.add_argument('--prefix',required=True)\np.parse_args()\n```")
    assert r.verdict == "skip"
    assert "requires arguments" in r.reason


@pytest.mark.asyncio
async def test_non_argparse_exit_2_still_fails(monkeypatch):
    """A real error that happens to exit 2 (no argparse usage/error) must still
    fail — the skip is narrow to argparse's required-args signature."""
    _patch_run(monkeypatch, CodeRunResult(
        ok=False, exit_code=2,
        stderr="Traceback (most recent call last):\n  ...\nValueError: bad config",
    ))
    r = await codegen_check.codegen_exec_smoke("```python\nraise ValueError('bad config')\n```")
    assert r.verdict == "fail"
