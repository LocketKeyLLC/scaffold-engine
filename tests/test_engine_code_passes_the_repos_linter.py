"""§17.1936 — code the engine writes for itself passes the repo's own linter (ruff, pyproject's rules): a gate on each
step's files, and before delivery a fix step for each module it refuses (once; the delivery waits on it).

Live (follow-up 5b4880c0, 2026-11-05): the delivered paper lab passed every gate and its 370 tests, and the commit
hook refused it -- a dict literal repeating `"spy_benchmark_return"`, a `zip()` without `strict=` -- in two modules
carried from the earlier job."""
from __future__ import annotations

import inspect
import shutil
import tomllib
from pathlib import Path

import pytest

from app.modules import engine_source as es
from app.modules import execution_agent as ea

ROOT = Path(__file__).resolve().parent.parent


def test_the_gate_holds_the_repos_rules():
    lint = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["ruff"]["lint"]
    assert tuple(lint["select"]) == es.LINT_SELECT and tuple(lint["ignore"]) == es.LINT_IGNORE
    assert tuple(lint["per-file-ignores"]["tests/**"]) == es.LINT_TESTS_IGNORE
    req, hook = ROOT / "requirements.txt", ROOT / ".pre-commit-config.yaml"    # not mounted in every test image
    if req.exists() and hook.exists():
        assert "ruff==0.16.3" in req.read_text() and "rev: v0.16.3" in hook.read_text()   # the hook's version


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff is not installed in this image")
@pytest.mark.asyncio
async def test_a_repeated_key_and_a_zip_without_strict_are_refused_a_clean_file_is_not():
    files = {"app/modules/paper_m.py": 'def f(a, b):\n    d = {"k": 1, "k": 2}\n    return d, list(zip(a, b))\n',
             "app/modules/paper_ok.py": "def g(a, b):\n    return list(zip(a, b, strict=True))\n",
             "tests/test_paper_m.py": "from app.modules.paper_m import *\n\n\ndef test_f():\n    assert f([], [])\n"}
    got = await es.lint_findings(files)
    assert [g.split(" -- ")[0] for g in got] == [
        '`app/modules/paper_m.py:2` ruff F601: Dictionary key literal `"k"` repeated',
        "`app/modules/paper_m.py:3` ruff B905: `zip()` without an explicit `strict=` parameter"]


@pytest.mark.asyncio
async def test_no_ruff_is_no_finding(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert await es.lint_findings({"app/modules/paper_m.py": "d = {'k': 1, 'k': 2}\n"}) == []


def test_the_gate_and_the_delivery_check_are_wired():
    assert "await lint_findings(" in inspect.getsource(es.engine_findings)
    assert "lint_fix_steps(job_id" in inspect.getsource(ea._pause_for_decision)
