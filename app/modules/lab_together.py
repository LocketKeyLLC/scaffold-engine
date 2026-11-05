"""§17.1933 — before a job's code is delivered, its tests are run together, as the engine's CI will run them.

Each code step's sandbox runs that step's own tests (§17.1765) against the job's finished modules. A step that
rewrites a module later is never held to the tests an EARLIER step wrote for it: those go stale while every step
passes. Live (follow-up 5b4880c0, 2026-11-05 14:15 UTC): the job parked at "Is this job's code merged into the engine
and running?" with every step done; the delivered branch ran 12 failing tests, all in T5's
`tests/test_paper_twelve_modules.py` (written 10-30 for modules seven fix steps rewrote since: `requests_made` gone,
`run_execution_entry_point` taking `db`, the Reddit client reading `REDDIT_USERNAME`).

So just before the delivery question is parked, the job's current test files run together in the sandbox. Each
failing file whose writer step writes tests only (its modules are other steps') gets that step read again: the run is
recorded as the step's own sandbox attempt (so the failing tests are not kept, §17.1923, and every passing one is),
the step is reset, and the delivery question waits on it. Once per step: a file still failing after its step ran
again is the operator's to see, named in the log, and the question is parked as before."""
from __future__ import annotations

import json
import logging

from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold")

#: a step's tool_config mark: its tests were read again for this (once)
RERAN_KEY = "stale_tests_reran"
#: the jobs whose steps the last check reset, until the run loop takes it (one event loop: a plain set)
_JUST_RESET: set[str] = set()


def take_reset(job_id: str) -> bool:
    """Whether the last check reset steps of this job (once: the run loop goes back to claim them)."""
    if job_id in _JUST_RESET:
        _JUST_RESET.discard(job_id)
        return True
    return False


def steps_to_read_again(tests: dict[str, tuple[str, str]], failed: list[str],
                        by_key: dict[str, dict]) -> list[tuple[str, str, list[str]]]:
    """(step, test file, failing test names) for each failing file whose writer is a tests-only step of this job not
    yet read again for this; the rest are logged (the operator's to see)."""
    from app.sandbox.engine_tests import files_of, is_test
    by_file: dict[str, list[str]] = {}
    for nid in failed:
        path, _, name = nid.partition("::")
        by_file.setdefault(path, []).append(name.rsplit("::", 1)[-1].split("[", 1)[0])
    out = []
    for path, names in sorted(by_file.items()):
        step = (tests.get(path) or ("", ""))[1]
        row = by_key.get(step) or {}
        written = files_of(row) if row else {}
        cfg = row.get("tool_config") or {}
        cfg = cfg if isinstance(cfg, dict) else json.loads(cfg)
        if ":" in step or not written or not all(is_test(p) for p in written) or cfg.get(RERAN_KEY):
            logger.warning("lab_tests_together_failing file=%s step=%s tests=%s -- not a tests-only step of this job, "
                           "or read again once already", path, step, names[:20])
            continue
        out.append((step, path, list(dict.fromkeys(names))))
    return out


async def stale_test_steps(job_id: str, deliver_key: str) -> list[str]:
    """Run the job's tests together; reset each tests-only step whose file fails. Returns the steps reset."""
    from app.modules import engine_source, node_editor
    from app.sandbox import engine_tests
    async with async_session() as db:
        rows = [dict(r) for r in await engine_source.code_rows(db, job_id)]
    files = engine_source.job_files(rows)
    tests = {p: (code, step) for p, (code, step) in files.items() if engine_tests.is_test(p)}
    if not tests:
        return []
    output = "\n\n".join(f"```python\n# {p}\n{code.rstrip()}\n```" for p, (code, _s) in tests.items())
    res = await engine_tests.check(output, {"node_key": f"{deliver_key}:together", "title": "the job's tests together"},
                                   rows)
    if res.verdict != "fail" or not res.failed:
        logger.info("lab_tests_together job=%s verdict=%s files=%d passed=%d", job_id, res.verdict, len(tests),
                    len(res.passed))
        return []
    from app.modules.engine_plan import _PYTEST_COUNTS_RE
    cm = next((m for m in _PYTEST_COUNTS_RE.finditer(res.reason or "") if any(m.groups())), None)
    # the count line marks the record as a sandbox verdict (`sandbox_counts`): the run's own, else one built from it
    counts = cm.group(0).strip() if cm else f"{len(res.failed)} failed, {len(res.passed)} passed in 0.0s"
    by_key = {str(r.get("node_key")): r for r in rows}
    reset: list[str] = []
    for step, path, names in steps_to_read_again(tests, res.failed, by_key):
        row = by_key[step]
        async with async_session() as db:
            node_id = (await db.execute(text("SELECT id FROM dag_nodes WHERE job_id = :j AND node_key = :k"),
                                        {"j": job_id, "k": step})).scalar()
            if node_id is None:
                continue
            # the together-run, recorded as the step's own sandbox attempt: `prior_passing_tests` then keeps what
            # passed and takes off what failed (a pytest count line in the reason marks it as a sandbox verdict)
            ok = [nid.partition("::")[2].rsplit("::", 1)[-1] for nid in res.passed if nid.startswith(path + "::")]
            await db.execute(text(
                "INSERT INTO execution_logs (job_id, node_id, log_level, message, details) "
                "VALUES (:j, :n, 'warning', :m, CAST(:d AS JSONB))"),
                {"j": job_id, "n": node_id, "m": f"Node '{row.get('title')}' (its tests run with the job's) -> failed",
                 "d": json.dumps({"reason": f"§17.1933 run with the job's tests before delivery ({counts}); in "
                                            f"`{path}` {len(names)} failed, {len(ok)} passed: " + ", ".join(names[:40]),
                                  "tests": {path: names}, "passed_tests": {}})})
            await node_editor._reset_keys(db, job_id, [step], edited_by="engine §17.1933")
            await db.execute(text(
                "UPDATE dag_nodes SET tool_config = COALESCE(tool_config, '{}'::jsonb) || CAST(:c AS JSONB) "
                "WHERE job_id = :j AND node_key = :k"),
                {"j": job_id, "k": step, "c": json.dumps({RERAN_KEY: names[:200]})})
            await db.execute(text(
                "UPDATE dag_nodes SET depends_on = array_append(depends_on, CAST(:k AS TEXT)) "
                "WHERE job_id = :j AND node_key = :d AND NOT (CAST(:k AS TEXT) = ANY(depends_on))"),
                {"j": job_id, "k": step, "d": deliver_key})
            await db.commit()
        reset.append(step)
        _JUST_RESET.add(job_id)
        logger.warning("lab_tests_stale_step_reset job=%s step=%s file=%s failed=%d", job_id, step, path, len(names))
    return reset
