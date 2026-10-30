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
#: §17.1838 — a block headed with another kind of file (`# verifier_response.json`, `# config.yaml`) is that file
_OTHER_FILE_RE = re.compile(r"^\s*#\s*(?:[\w.-]+/)*[\w.-]+\.(?!py\b)[A-Za-z0-9]{1,8}\s*$")


@dataclass
class EngineTestResult:
    verdict: str          # "pass" | "fail" | "skip"
    reason: str
    ran: list[str]


def record(res: EngineTestResult) -> dict:
    """§17.1810 — the verdict as the step keeps it: whether its own tests ran, or only an import of its module."""
    kind = "tests" if any(is_test(p) for p in res.ran) else ("import" if res.ran else "none")
    return {"verdict": res.verdict, "kind": kind, "ran": list(res.ran), "reason": (res.reason or "")[:500]}


def files_of(row: dict) -> dict[str, str]:
    """The files one step writes: each Python block at the path its `# path.py` header names, else (the first
    unnamed block) at the step's own module -- `app/modules/x.py`, or `tests/test_x.py` for a test module."""
    from app.modules.engine_source import step_module
    out: dict[str, str] = {}
    unnamed_used = False
    for lang, code in extract_code_blocks(str(row.get("output_text") or "")):
        if lang not in PYTHON_LANGS or not code.strip():
            continue
        first = code.lstrip().splitlines()[0]
        m = _HEADER_RE.match(first)
        named = bool(m)
        if m:
            path = m.group(1)
        elif _OTHER_FILE_RE.match(first):
            continue                                        # §17.1838 — not a Python file, never the step's module
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
        if not named and path in out:
            continue                                        # §17.1838 — an unnamed block never replaces a named one
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


#: §17.1823 — failures the engine recognises, with what fixes them, put next to the error rather than left to a rule
#: far up the prompt. Live (follow-up 5b4880c0): a dict bound to a JSON column through `text()` was the cause of 20 of
#: the job's 71 sandbox refusals, across six steps, though the tables block states the rule in every prompt.
KNOWN_CAUSES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"descriptor 'encode' for 'str' objects doesn't apply to a '(?:dict|list)' object"
                r"|invalid input for query argument \$\d+: [\[{]"),
     "a Python dict or list was bound to a JSON/JSONB column through `text()`: asyncpg takes JSON only as text -- "
     "bind `json.dumps(value)` and write `CAST(:p AS json)` (or `jsonb`, the column's own type) in the SQL; in the "
     "tests' INSERTs too"),
    (re.compile(r"'MagicMock' object can't be awaited|object MagicMock can't be used in 'await' expression"),
     "an awaited call was mocked with `MagicMock`: use `AsyncMock` for anything the code awaits (`db.execute`, "
     "`db.commit`, a client's coroutine methods) -- or test against the scratch database instead of a fake session"),
    # §17.1853 — live (5b4880c0 T6, 2026-10-28): `write_order(approval_id=1, …)` with no `approval` row.
    # §17.1866 — or a parent the test inserted in its own session and never committed (live 5b4880c0 T5, 2026-10-29:
    # the attempts whose helpers committed passed the same tests)
    (re.compile(r"violates foreign key constraint"),
     "a row was inserted with a foreign key to a parent row that does not exist: insert the parent row first -- with "
     "its writer in the job's modules, or an INSERT -- and pass the id it returned, never a made-up one; and when the "
     "parent was inserted in the test's own session, `await db.commit()` before calling a writer that opens its own "
     "`async_session()` -- it sees only committed rows"),
    # §17.1857 — live (5b4880c0 T5 and T8, 2026-10-28): the EDGAR ingester and the broker client ran for real
    (re.compile(r"Missing operator secret|SecretsMissingError|Missing \w[\w ]* secrets?:"),
     "the code ran for real and asked for an operator secret the sandbox does not have: patch `values_for` where the "
     "module looks it up (`app.modules.<module>.values_for`) to return a test value for each name it reads"),
    # §17.1858 — live (5b4880c0 T5, 2026-10-28): `approval` written with no `proposal_id`
    (re.compile(r"violates not-null constraint"),
     "a row was written without a column that has no default: the test passes it -- for an `_id` column, the id the "
     "parent's writer returned"),
    # §17.1870 — live (5b4880c0 T5, 2026-10-30): the finished FINRA ingester calls `paper_audit_trail_writer.write`,
    # which does not exist -- its tests failed inside the module, three attempts running, and nothing could fix it there
    (re.compile(r"AttributeError: module 'app\.modules\.\w+' has no attribute '\w+'|NameError: name '\w+' is not defined"),
     "a finished module reaches for a name that does not exist (a module's missing attribute, an undefined name): that "
     "module is at fault, not the test -- when it is another step's module, the job gets a fix step for it and this "
     "step waits"),
)


_MISSING_SECRET = re.compile(r"Missing operator secret (\w+)|Missing [\w ]*secrets?: ([\w, ]+)")
_NULL_COLUMN = re.compile(r'null value in column "(\w+)" of relation "(\w+)"')


_MISSING_PARENT = re.compile(r'Key \((\w+)\)=\((.*?)\) is not present in table "(\w+)"')


_MOCK_AWAITED = KNOWN_CAUSES[1][0]
_AWAIT_EXPR = re.compile(r"\bawait\s+([^\s(]+)\(")


def _awaited_mocks(text: str) -> list[str]:
    """§17.1825 — each call the run awaited on a `MagicMock`, as ``db.close()`` (file:line), from the traceback.

    pytest's short traceback puts the failing source line right above the ``E`` line and its ``path:N: in f``
    above that. Live (follow-up 5b4880c0 T6, 2026-10-26): the hint named `db.execute`/`db.commit`, the code awaited
    `db.close()`, and three attempts in a row fixed the named calls and kept the mocked close."""
    lines = (text or "").splitlines()
    found: list[str] = []
    for i, ln in enumerate(lines):
        if not (ln.lstrip().startswith("E ") and _MOCK_AWAITED.search(ln)) or i < 1:
            continue
        m = _AWAIT_EXPR.search(lines[i - 1])
        if not m:
            continue
        where = re.match(r"\s*([\w./-]+\.py:\d+):", lines[i - 2]) if i >= 2 else None
        item = f"`{m.group(1)}()`" + (f" ({where.group(1)})" if where else "")
        if item not in found:
            found.append(item)
    return found


#: §17.1831 — the recognised causes that are the fault of the function that raised them (a dict bound to a JSON
#: column is the binder's bug), not of the test that called it (an awaited `MagicMock` is the test's).
CALLEE_CAUSES = (KNOWN_CAUSES[0], KNOWN_CAUSES[5])                     # §17.1870 -- and a name the module lacks
#: §17.1871 — pytest pads a failure's header to the terminal width with underscores: a long test name gets ONE on
#: each side. Live (5b4880c0 T5, 2026-10-30 04:16 UTC): `_ TestOperatorApprovalGate.test_handle_…_write_order _`
#: did not open a section, two failures merged, and the FINRA module's missing name was pinned on the next test's
#: deepest frame (`paper_audit_trail_writer.py:32 in write_signal`) -- a fix step for a module with nothing to fix.
_SECTION_RE = re.compile(r"^_+ (\S.*?) _+$")
_APP_FRAME_RE = re.compile(r"^(app/modules/(\w+)\.py):(\d+): in (\w+)$")


def callee_defects(report: str) -> list[dict]:
    """§17.1831 — each failure of a run whose cause is a `CALLEE_CAUSES` fault raised inside an engine module, as
    ``{module, frame, test, cause}`` -- the module is the DEEPEST ``app/modules`` frame of that failure. Live
    (follow-up 5b4880c0 T6, 2026-10-26): the batch's tests ran through f00ea97f:T4's ``write_risk_check``, which binds
    its ``metrics: dict`` straight to a JSONB column; every attempt failed at ``paper_audit_trail_writer.py:131``, a
    file the step may not rewrite, and the hint told it to ``json.dumps`` its own values."""
    out: list[dict] = []
    test, frame, errors = "", None, []

    def close():
        hit = next((hint for rx, hint in CALLEE_CAUSES for e in errors if rx.search(e)), None)
        if frame and hit:
            out.append({"module": frame[1], "frame": f"{frame[0]}:{frame[2]} in {frame[3]}", "test": test,
                        "cause": hit})

    for ln in (report or "").splitlines():
        if (m := _SECTION_RE.match(ln.strip())) or "short test summary info" in ln:
            close()
            test, frame, errors = (m.group(1) if m else ""), None, []
        elif (f := _APP_FRAME_RE.match(ln.strip())):
            frame = f.groups()
        elif ln.startswith("E "):
            errors.append(ln)
    close()
    return out


def known_causes(text: str) -> str:
    """§17.1823 — the recognised causes in a failed run's output, one line each (empty when none)."""
    hits = [hint for rx, hint in KNOWN_CAUSES if rx.search(text or "")]
    if (calls := _awaited_mocks(text)) and KNOWN_CAUSES[1][1] in hits:
        i = hits.index(KNOWN_CAUSES[1][1])
        hits[i] += "; this run awaited a `MagicMock` at " + ", ".join(calls)
    if KNOWN_CAUSES[2][1] in hits and (gone := list(dict.fromkeys(
            f"`{c}`={v} has no `{t}` row" for c, v, t in _MISSING_PARENT.findall(text or "")))):
        hits[hits.index(KNOWN_CAUSES[2][1])] += "; this run: " + ", ".join(gone)       # §17.1853
    if KNOWN_CAUSES[3][1] in hits and (names := list(dict.fromkeys(
            n for a, b in _MISSING_SECRET.findall(text or "") for n in ([a] if a else [x.strip() for x in b.split(",")])
            if n))):
        hits[hits.index(KNOWN_CAUSES[3][1])] += "; this run: " + ", ".join(f"`{n}`" for n in names)   # §17.1857
    if KNOWN_CAUSES[4][1] in hits and (cols := list(dict.fromkeys(
            f"`{t}.{c}` was null" for c, t in _NULL_COLUMN.findall(text or "")))):
        hits[hits.index(KNOWN_CAUSES[4][1])] += "; this run: " + ", ".join(cols)        # §17.1858
    return "".join(f"\nCause the engine recognises: {h}." for h in hits)


#: A traceback frame header that is not the job's code: an installed library, a compiled extension, the stdlib.
_LIBRARY_FRAME = re.compile(r"^(?:/\S*/(?:site-packages|lib/python3\.\d+)/\S+|\S+\.pyx|<frozen [^>]+>):\d+: in ")


def _fold_library_frames(lines: list[str]) -> list[str]:
    """§17.1826 — each run of library frames (header + its indented source and ``^^^`` lines) as ONE line.

    Live (follow-up 5b4880c0 T6, 2026-10-26): a foreign-key error raised through SQLAlchemy and asyncpg printed 31
    library frames; they filled the budget and the run's second failure never reached the next attempt. The job's
    own frames (``tests/…``, ``app/…``) and every ``E`` line are kept as they are."""
    out: list[str] = []
    folded = 0
    for ln in lines:
        if _LIBRARY_FRAME.match(ln):
            folded += 1
            continue
        if folded and (ln.startswith((" ", "\t")) or ln.lstrip().startswith("^")):
            continue                                        # the folded frame's source line / its markers
        if folded:
            out.append(f"  … {folded} library frame{'s' if folded > 1 else ''} …")
            folded = 0
        out.append(ln)
    if folded:
        out.append(f"  … {folded} library frame{'s' if folded > 1 else ''} …")
    return out


_FAILURE_HEAD = re.compile(r"^_+ (\S.*?) _+$")                            # §17.1871 -- one underscore is enough


def _failure_index(lines: list[str], limit: int = 80) -> list[str]:
    """§17.1869 — every failure on one line: `test_x: E   AttributeError: …` (the first error line under its header),
    so a run with fifty failures names all fifty before the tracebacks the budget keeps. Live (5b4880c0 T5,
    2026-10-30): 50 of 70 failed; the refusal carried six of them."""
    out: list[str] = []
    name = ""
    for ln in lines:
        m = _FAILURE_HEAD.match(ln.strip())
        if m:
            if name:
                out.append(f"{name}: (no error line)")
            name = m.group(1)
        elif name and ln.startswith("E ") and not ln.startswith("E   DETAIL") and "direct cause" not in ln:
            out.append(f"{name}: {ln[:160]}")
            name = ""
    if name:
        out.append(f"{name}: (no error line)")
    return out[:limit] + ([f"… and {len(out) - limit} more"] if len(out) > limit else [])


def _report(text: str, budget: int = 9000) -> str:
    """§17.1817 — a failed test run as the next attempt needs it: every failure's short traceback from the FIRST one
    (often the cause the rest follow from), then pytest's summary. Live (follow-up 5b4880c0 T6, 8-14 of its tests
    failing per attempt): the last 60 lines of long tracebacks held one failure and a summary cut at 80 columns
    ("- sqlalc..."), so each attempt was redrafted from one failure out of ten. §17.1869 — when the budget cuts,
    an index of every failure (one line each) leads, so none goes unnamed."""
    lines = _fold_library_frames([ln for ln in (text or "").splitlines() if ln.strip()])
    whole = "\n".join(lines)
    if len(whole) <= budget:
        return whole
    index = _failure_index(lines)
    head = ("Every failure, one line each (the tracebacks below hold the first of them):\n" + "\n".join(index) + "\n\n") \
        if len(index) > 1 else ""
    budget = max(budget - len(head), budget // 2)
    cut = next((i for i, ln in enumerate(lines) if "short test summary info" in ln), len(lines))
    summary = "\n".join(lines[cut:])[-(budget // 4):]
    return head + "\n".join(lines[:cut])[: budget - len(summary) - 3] + "\n…\n" + summary


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
        command = ["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", "-o", "asyncio_mode=auto",
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
    elif tests:
        why = _report(res.stdout + "\n" + res.stderr)                               # §17.1817
    else:
        why = _tail(res.stdout + "\n" + res.stderr)
    what = "its tests" if tests else "importing its module" + ("s" if len(ran) > 1 else "")
    return EngineTestResult("fail", (
        f"§17.1765 engine-test sandbox: {what} failed, run against the engine with this job's finished modules "
        f"({', '.join(sorted(p for p in files if p not in mine and not p.endswith('__init__.py'))) or 'none yet'}). "
        + ("The sandbox has no network and no secrets; `async_session` is a fresh scratch database with the engine's "
           "schema and this job's migrations applied, and each test starts with this job's own tables empty (§17.1819) "
           "-- a test inserts the rows it needs; test SQL against it instead of a fake session that ignores "
           "the query; mock HTTP clients and model calls. " if res.database == "ok" else
           "The sandbox has no network, no database and no secrets: mock `async_session`, HTTP clients and model calls "
           "in tests. ")
        + "Fix the code (or the test, when the test is what is wrong):"
        + known_causes(res.stdout + "\n" + res.stderr) + "\n\n" + why[:12000]), ran)
