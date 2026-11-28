"""§17.2062–2064 — the integration tests run somewhere, and say why when they fail.

§17.2062  Tier 2 (``integration.yml``) is gated off: it targets a self-hosted
          runner on the engine host behind ``vars.RUN_TIER2_INTEGRATION`` and
          its first step rebuilds the LIVE orchestrator. ``make test-integration``
          execs into the live container, whose production ``DATABASE_URL`` the
          §17.1108 guard refuses. The only lane that ran ``tests/integration``
          was test.yml's Postgres-only step — and its ``--ignore-glob='*_live*'``
          also dropped ``test_assist_live_operator_loop.py``, a Postgres-only
          file whose failing control (§17.2054) nobody saw. Gated here: the CI
          lane's exclusions are exactly the named files with reasons, and
          ``make test-integration-lane`` runs the whole directory in a throwaway
          container against the test DB.

§17.2063  ``make openapi-check`` exec'd into the live orchestrator, so it checked
          whatever /code the RUNNING app had — "docs/openapi.json is missing"
          from a worktree. It now checks the working tree.

§17.2064  A live test's engine/helper logs died with the throwaway container,
          so one failure on main 92c26e33 could not be explained. A failing
          test in a module that declares ``ITEST_LOGS`` now carries their tail.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTEG = ROOT / "tests" / "integration"
_TEST_YML = ROOT / ".github" / "workflows" / "test.yml"

# Every tests/integration file the Postgres-only CI lane may skip, and why.
# A file NOT listed here is collected by CI; adding one needs a reason.
CI_LANE_EXCLUSIONS = {
    "test_retrieval_eval.py": "reads the Milvus corpus",
    "test_milvus_schema_parity.py": "introspects a Milvus collection",
    "test_auto_mode_live.py": "own engine + helper, real model (Ollama)",
    "test_runner_loop_live.py": "own engine + helper, real model (Ollama)",
    "test_detached_run_live.py": "own engine, real inference on every node",
    "test_diagnosis_accuracy_live.py": "real model + SearXNG",
    "test_codegen_golden_live.py": "real model (Ollama)",
    "test_spec_extractor_live.py": "real model (Ollama)",
}


def _recipe(target: str) -> str:
    src = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)*)", src, re.M)
    assert m, f"target {target} not found"
    return m.group(0)


def _ci_integration_step() -> str:
    wf = _TEST_YML.read_text(encoding="utf-8")
    m = re.search(r"- name: Integration lane[^\n]*\n(.*?)(?=\n      - name: |\Z)", wf, re.S)
    assert m, "test.yml's Postgres-only integration lane step not found"
    return m.group(1)


# ── §17.2062: the CI lane ────────────────────────────────────────────────────

@pytest.mark.skipif(not _TEST_YML.exists(), reason="host-only static gate — .github not mounted")
def test_ci_integration_lane_excludes_exactly_the_named_files():
    step = _ci_integration_step()
    assert "--ignore-glob" not in step, (
        "a glob exclusion swallowed test_assist_live_operator_loop.py (§17.2062); "
        "name each excluded file instead")
    ignored = set(re.findall(r"--ignore=tests/integration/(\S+\.py)", step))
    assert ignored == set(CI_LANE_EXCLUSIONS), (
        f"CI lane exclusions drifted from the reasoned list: "
        f"extra={sorted(ignored - set(CI_LANE_EXCLUSIONS))} "
        f"missing={sorted(set(CI_LANE_EXCLUSIONS) - ignored)}")
    for name in ignored:
        assert (INTEG / name).exists(), f"excluded file {name} no longer exists — drop it"


@pytest.mark.skipif(not _TEST_YML.exists(), reason="host-only static gate — .github not mounted")
def test_ci_integration_lane_collects_the_operator_loop():
    """The regression itself: the §17.2054 control lives in this file."""
    import fnmatch

    step = _ci_integration_step()
    assert "pytest tests/integration/" in step
    target = "tests/integration/test_assist_live_operator_loop.py"
    assert (ROOT / target).exists()
    globs = re.findall(r"--ignore-glob=['\"]?([^'\"\s]+)", step)
    paths = re.findall(r"--ignore=(\S+)", step)
    swallowed = [g for g in globs if fnmatch.fnmatch(target, g) or fnmatch.fnmatch(Path(target).name, g)]
    assert not swallowed, f"{target} is excluded from CI by {swallowed} (§17.2062)"
    assert target not in paths
    assert Path(target).name not in CI_LANE_EXCLUSIONS


def test_integration_lane_target_is_throwaway_and_whole():
    block = _recipe("test-integration-lane")
    head = block.splitlines()[0]
    assert re.search(r":\s*test-db\b", head), "the lane provisions the test DB first"
    assert "docker exec" not in block, "the lane must never exec into the live orchestrator"
    assert "$(_TEST_RUN_PRE)" in block, "the lane runs in the throwaway test-DB container"
    assert "flock" in block and "$(TEST_DB_LOCK)" in block, "the lane shares the test-DB lock"
    assert re.search(r"pytest tests/integration(\s|$)", block), "the lane runs the WHOLE directory"
    assert "--ignore" not in block and " -m " not in block, "the lane must not drop files"
    assert "ITEST_LOG_DIR=/itest" in block and "/itest" in block, "logs must survive the container"


# ── §17.2063: openapi-check reads the working tree ───────────────────────────

def test_openapi_check_checks_the_working_tree():
    block = _recipe("openapi-check")
    assert "docker exec" not in block, "openapi-check must not read the live container's /code (§17.2063)"
    assert "$(CURDIR):/code" in block, "openapi-check mounts the working tree"
    assert "openapi_snapshot.py --check" in block
    assert "--env-file" not in block, "CI's §17.175 check runs without .env; a worktree may have none"


# ── §17.2064: a failing live test prints its subprocess logs ─────────────────

_LOG_LITERAL = re.compile(r"""["']/?(itest_[A-Za-z0-9_]+\.log)["']""")


def _declared_logs(path: Path) -> tuple[str, ...] | None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ITEST_LOGS" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    return None


@pytest.mark.parametrize("path", sorted(INTEG.glob("test_*.py")), ids=lambda p: p.name)
def test_every_module_that_writes_a_subprocess_log_declares_it(path):
    written = set(_LOG_LITERAL.findall(path.read_text(encoding="utf-8")))
    declared = _declared_logs(path)
    if not written:
        assert declared is None, f"{path.name} declares ITEST_LOGS but writes no itest_*.log"
        return
    assert declared is not None, f"{path.name} writes {sorted(written)} but declares no ITEST_LOGS"
    assert set(declared) == written, f"{path.name}: ITEST_LOGS={declared} vs written={sorted(written)}"


def test_the_live_modules_this_arc_names_declare_logs():
    """Bite check for the parity test above: the scan must find these."""
    for name in ("test_auto_mode_live.py", "test_runner_loop_live.py",
                 "test_detached_run_live.py", "test_diagnosis_accuracy_live.py"):
        assert _declared_logs(INTEG / name), name


def _run_hook(hook, item, report):
    gen = hook(item, SimpleNamespace(when=report.when))
    next(gen)
    with pytest.raises(StopIteration):
        gen.send(SimpleNamespace(get_result=lambda: report))


def test_a_failing_report_carries_the_log_tail(tmp_path, monkeypatch):
    from tests.integration import conftest as ic

    (tmp_path / "itest_x_engine.log").write_text(
        "".join(f"line {i}\n" for i in range(400)) + "Traceback: the engine's side\n")
    monkeypatch.setenv("ITEST_LOG_DIR", str(tmp_path))
    item = SimpleNamespace(module=SimpleNamespace(ITEST_LOGS=("itest_x_engine.log", "itest_missing.log")))

    failed = SimpleNamespace(failed=True, when="call", sections=[])
    _run_hook(ic.pytest_runtest_makereport, item, failed)
    titles = [t for t, _ in failed.sections]
    assert any("itest_x_engine.log" in t for t in titles)
    body = dict(failed.sections)[titles[0]]
    assert body.endswith("Traceback: the engine's side")
    assert "line 0\n" not in body, "only the tail is attached"
    assert len(body.splitlines()) == ic.ITEST_LOG_TAIL_LINES
    assert "no such file" in dict(failed.sections)[titles[1]]

    passed = SimpleNamespace(failed=False, when="call", sections=[])
    _run_hook(ic.pytest_runtest_makereport, item, passed)
    assert passed.sections == [], "a passing test attaches nothing"

    bare = SimpleNamespace(failed=True, when="call", sections=[])
    _run_hook(ic.pytest_runtest_makereport, SimpleNamespace(module=SimpleNamespace()), bare)
    assert bare.sections == [], "a module without ITEST_LOGS attaches nothing"
