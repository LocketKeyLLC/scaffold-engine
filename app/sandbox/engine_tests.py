"""§17.1765 — a code step written for the engine is RUN before it passes: its tests, or an import of its module,
against the engine with this job's other modules written in.

Live (job 2772acae): ~24 logic defects (a frozen dataclass mutated, filings read from the wrong key so every result
was [], a market order's notional 0 so the 5% check always passed) and an integration-test step that failed three
times on test-design bugs no reader saw -- nothing ever ran the lab's code. The coderunner (§17.434) holds python and
pytest only, so `import app.…` is a skip there. The engine-test sandbox (`docker/enginetest/`) is the engine's own
dev image on an internal network: no route out, no database, no secrets.

  pass -- the step's tests passed (or, for a module with no tests, it imports cleanly with its siblings present)
  fail -- they did not; the tail of pytest's output is the reason the next attempt is drafted from
  skip -- the sandbox is off, unreachable, or holds another engine (its app/ digest differs from the one running
          here): logged loudly, never a pass recorded as tested
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.config import settings
from app.modules.execution_codegen_gate import PYTHON_LANGS, extract_code_blocks
from app.sandbox.client import run_code

logger = logging.getLogger("scaffold")

#: a file named in the block's first line; an Alembic revision's file starts with digits (`0010_paper_audit_trail.py`)
_HEADER_RE = re.compile(r"^\s*#\s*((?:[\w.-]+/)*\w+\.py)\b")
_TEST_NAME_RE = re.compile(r"(?:^|/)(?:test_\w+|\w+_test|conftest)\.py$")


@dataclass
class EngineTestResult:
    verdict: str          # "pass" | "fail" | "skip"
    reason: str
    ran: list[str]


def files_of(row: dict) -> dict[str, str]:
    """The files one step writes: each Python block at the path its `# path.py` header names, else (the first
    unnamed block) at the step's own module -- `app/modules/x.py`, or `tests/test_x.py` for a test module."""
    from app.modules.engine_source import step_module
    out: dict[str, str] = {}
    unnamed_used = False
    for lang, code in extract_code_blocks(str(row.get("output_text") or "")):
        if lang not in PYTHON_LANGS or not code.strip():
            continue
        m = _HEADER_RE.match(code.lstrip().splitlines()[0])
        if m:
            path = m.group(1)
        elif not unnamed_used:
            leaf = step_module(row)
            if not leaf:
                continue
            path = f"{leaf}.py"
            unnamed_used = True
        else:
            continue
        if "/" not in path:
            path = f"tests/{path}" if _TEST_NAME_RE.search(path) else f"app/modules/{path}"
        if path.split("/")[0] not in ("app", "tests") and not path.startswith("alembic/versions/"):
            continue                                        # a script: not imported by a test
        # §17.1783 — a job's Alembic revision IS carried: the sandbox applies it to the run's scratch database
        out[path] = code
    return out


def is_test(path: str) -> bool:
    return bool(_TEST_NAME_RE.search(path))


def _module_name(path: str) -> str:
    return path[:-3].replace("/", ".")


def _local_digest() -> str:
    from app.modules.engine_source import ENGINE_PKG
    return _digest_of(str(ENGINE_PKG))


@lru_cache(maxsize=4)
def _digest_of(root: str) -> str:
    """The same digest the sandbox's /health reports (docker/enginetest/server.py:engine_digest)."""
    from pathlib import Path
    h = hashlib.sha256()
    base = Path(root)
    for p in sorted(base.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        h.update(str(p.relative_to(base)).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


async def _sandbox_engine(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{url.rstrip('/')}/health")
            r.raise_for_status()
            return str(r.json().get("engine") or "")
    except Exception as exc:                                # noqa: BLE001 — unreachable is a skip, logged
        logger.warning("engine_test_sandbox_unreachable url=%s err=%r", url, exc)
        return None


def _tail(text: str, lines: int = 60) -> str:
    keep = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(keep[-lines:])


async def check(output: str, node: dict, siblings: list[dict]) -> EngineTestResult:
    """Run this step's tests (or import its modules) with its finished siblings' files present."""
    url = (settings.engine_test_url or "").strip()
    if not (settings.engine_test_check_enabled and url):
        return EngineTestResult("skip", "engine-test sandbox off", [])
    mine = files_of({**node, "output_text": output})
    if not mine:
        return EngineTestResult("skip", "no Python file to run", [])
    files: dict[str, str] = {}
    for r in siblings:
        if r.get("status") == "done" and r.get("node_key") != node.get("node_key"):
            files.update(files_of(r))
    files.update(mine)
    for p in list(files):                                   # every folder a test imports through is a package
        if p.startswith("alembic/"):
            continue        # §17.1783 — never: an `alembic/__init__.py` shadows the alembic library itself
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            init = "/".join(parts[:i]) + "/__init__.py"
            if init.startswith("app/modules") or init == "app/__init__.py":
                continue                                    # the engine's own packages are already there
            files.setdefault(init, "")
    there = await _sandbox_engine(url)
    if there is None:
        return EngineTestResult("skip", "engine-test sandbox unreachable", [])
    here = _local_digest()
    if there != here:
        logger.error("engine_test_sandbox_stale sandbox=%s engine=%s -- rebuild it (make build)", there, here)
        return EngineTestResult("skip", f"engine-test sandbox holds another engine ({there} vs {here})", [])
    tests = sorted(p for p in mine if is_test(p))
    if tests:
        command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "asyncio_mode=auto",
                   "-o", "asyncio_default_fixture_loop_scope=function", "--timeout=60", "-c", "/dev/null",
                   "--rootdir", ".", "-W", "ignore::DeprecationWarning", *tests]
    else:
        mods = [_module_name(p) for p in sorted(mine) if p.startswith("app/")]
        if not mods:
            return EngineTestResult("skip", "no module to import", [])
        command = ["python", "-c", "import importlib, sys\n"
                   f"for m in {mods!r}:\n    importlib.import_module(m)\nprint('imported', len({mods!r}))"]
    res = await run_code(files, command, timeout_s=settings.engine_test_timeout_s,
                         cpu_seconds=int(settings.engine_test_timeout_s) + 10, mem_mb=1536,
                         base_url=url, database=True)
    if res.database.startswith("unavailable"):
        logger.warning("engine_test_database_unavailable node=%s %s", node.get("node_key"), res.database[:200])
    ran = tests or [p for p in mine if p.startswith("app/")]
    if res.error is not None:
        return EngineTestResult("skip", f"engine-test sandbox: {res.error}", ran)
    if res.ok:
        return EngineTestResult("pass", ("tests passed" if tests else "imports cleanly") + f" ({', '.join(ran)})", ran)
    if res.timed_out:
        why = f"timed out after {settings.engine_test_timeout_s:.0f}s (a test waiting on a network or database it does not have?)"
    elif tests and res.exit_code == 5:
        why = "pytest collected no tests: name them `def test_…` / `async def test_…` at module level"
    else:
        why = _tail(res.stdout + "\n" + res.stderr)
    what = "its tests" if tests else "importing its module" + ("s" if len(ran) > 1 else "")
    return EngineTestResult("fail", (
        f"§17.1765 engine-test sandbox: {what} failed, run against the engine with this job's finished modules "
        f"({', '.join(sorted(p for p in files if p not in mine and not p.endswith('__init__.py'))) or 'none yet'}). "
        + ("The sandbox has no network and no secrets; `async_session` is a fresh scratch database with the engine's "
           "schema and this job's migrations applied -- test SQL against it instead of a fake session that ignores "
           "the query; mock HTTP clients and model calls. " if res.database == "ok" else
           "The sandbox has no network, no database and no secrets: mock `async_session`, HTTP clients and model calls "
           "in tests. ")
        + "Fix the code (or the test, when the test is what is wrong):\n\n" + why[:6000]), ran)
