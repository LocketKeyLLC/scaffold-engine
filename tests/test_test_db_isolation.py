"""§17.1108 (Phase 1 ledger L-2) — the test suite never runs against the
production database.

Two halves, both gated here:

  * ``tests/_live_write_guard.explain_non_test_database`` — conftest refuses
    to start when DATABASE_URL names a database that does not end in ``_test``.
  * the Makefile contract — no unit lane ``docker exec``s pytest into the live
    orchestrator any more; every lane is a throwaway container on the test DB,
    and the integration lane is explicit.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import _live_write_guard as g

ROOT = Path(__file__).resolve().parents[1]


# ── the URL judgement ────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,name", [
    ("postgresql+asyncpg://u:p@scaffold-postgres:5432/scaffold_engine", "scaffold_engine"),
    ("postgresql+asyncpg://u:p@localhost:5432/scaffold_engine_test", "scaffold_engine_test"),
    ("postgresql://u:p@h/scaffold_engine_test?ssl=require", "scaffold_engine_test"),
    ("postgresql://u:p@h", ""),
])
def test_database_name_parses_the_last_path_segment(url, name):
    assert g.database_name(url) == name


def test_production_database_is_refused(monkeypatch):
    monkeypatch.delenv(g._ENV_ESCAPE, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@scaffold-postgres:5432/scaffold_engine")
    msg = g.explain_non_test_database()
    assert msg and "REFUSING TO RUN" in msg and "'scaffold_engine'" in msg
    assert "make test-integration" in msg, "the refusal must name the sanctioned live lane"


def test_test_database_is_allowed(monkeypatch):
    monkeypatch.delenv(g._ENV_ESCAPE, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@scaffold-postgres:5432/scaffold_engine_test")
    assert g.explain_non_test_database() is None


def test_no_database_url_is_not_judged(monkeypatch):
    """Host-side static lanes (ci-tier-0, ci-smoke) set no DATABASE_URL and
    never open a session — they must keep running."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert g.explain_non_test_database() is None


def test_explicit_escape_hatch_disables_the_refusal(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@scaffold-postgres:5432/scaffold_engine")
    monkeypatch.setenv(g._ENV_ESCAPE, "1")
    assert g.explain_non_test_database() is None


def test_conftest_refuses_before_importing_app():
    """The check must precede `import app` so no engine is ever built against
    the wrong URL — order in the file, not just presence."""
    src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    i_guard = src.index("explain_non_test_database()")
    i_exit = src.index("_pytest.exit(_non_test_db")
    i_app = src.index("import app  # noqa: F401")
    assert i_guard < i_exit < i_app


# ── the Makefile contract ────────────────────────────────────────────────────

def _recipe(target: str) -> str:
    """The recipe lines of a Makefile target (tab-indented block after `target:`)."""
    src = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n|\n)*)", src, re.M)
    assert m, f"target {target} not found"
    return m.group(0)


@pytest.mark.parametrize("target", ["test", "test-pipelines", "coverage", "test-cli", "test-sdk", "agent"])
def test_unit_lanes_never_exec_into_the_live_orchestrator(target):
    block = _recipe(target)
    head = block.splitlines()[0]
    assert not re.search(r"\b_ensure_dev\b(?!_image)", head), (
        f"{target} still swaps the live orchestrator to the dev image")
    assert "docker exec $(CONTAINER)" not in block, f"{target} still runs inside the live container"
    assert "$(_TEST_RUN)" in block, f"{target} must use the throwaway-container macro"


def test_core_lane_excludes_integration_and_uses_the_test_db():
    src = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "TEST_DB_NAME ?= scaffold_engine_test" in src
    # §17.1153 — the macro is split: _TEST_RUN_PRE (flags + mounts) + the image, so a lane can add flags before the image
    assert re.search(r'_TEST_RUN_PRE = docker run .*-e DATABASE_URL="\$\(TEST_DB_URL\)"', src)
    assert "_TEST_RUN = $(_TEST_RUN_PRE) scaffold-engine:dev" in src
    assert '-m "not integration"' in _recipe("test")
    assert "test: test-db" in src, "the core lane provisions the test DB first"


def test_unit_lane_never_loads_the_real_reranker():
    """§17.1129 — the lifespan prewarm inside every ``TestClient(app)`` was
    loading the REAL CrossEncoder in unit tests: a Hub download in CI, and a
    permission-denied on the root-owned repo cache locally that flagged the
    loader 'down' for 5 minutes and failed the health tests that ran after
    it. The lane disables the prewarm and Hub access outright."""
    src = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(r"_TEST_RUN_PRE = docker run(?:.*\\\n)*.*-w /code\n_TEST_RUN = \$\(_TEST_RUN_PRE\) scaffold-engine:dev", src)
    assert m, "_TEST_RUN definition not found"
    assert "-e SCAFFOLD_PREWARM_RERANKER=false" in m.group(0)
    assert "-e HF_HUB_OFFLINE=1" in m.group(0)


def test_integration_lane_is_explicit_and_loud():
    block = _recipe("test-integration")
    assert "-m integration" in block
    assert "LIVE" in block, "the integration lane must say it drives the live engine"


# `.github/` is not mounted into the container lane (the image carries a baked,
# possibly stale copy) — same rule as test_ci_mount_parity: host-only, runs in
# `make ci-tier-0`.
_CI_WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


@pytest.mark.skipif(not (ROOT / "docker-compose.dev.yml").exists() or not _CI_WORKFLOW.exists(),
                    reason="host-only static gate — runs in `make ci-tier-0`, not the container lane")
def test_ci_uses_a_test_named_database():
    wf = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "scaffold_engine_test" in wf
    assert not re.search(r"\bscaffold_engine\b", wf), "CI's throwaway Postgres must carry the _test name too"


@pytest.mark.skipif(not (ROOT / "docker-compose.dev.yml").exists() or not _CI_WORKFLOW.exists(),
                    reason="host-only static gate — runs in `make ci-tier-0`, not the container lane")
def test_ci_unit_jobs_never_load_the_real_reranker():
    """§17.1129 — same rule as the local lane (see test_unit_lane_never_loads_the_real_reranker)."""
    wf = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r'^\s+SCAFFOLD_PREWARM_RERANKER: "false"$', wf, re.M)
    assert re.search(r'^\s+HF_HUB_OFFLINE: "1"$', wf, re.M)


# ── §17.1141 (ledger O-2) — the advisory-tool zero state is gated ────────────

def test_the_audit_gate_exists_and_checks_all_three_tools():
    src = (ROOT / "Makefile").read_text(encoding="utf-8")
    block = _recipe("audit-gate")
    for tool in ("pyright app --level error", "vulture app scripts/vulture_whitelist.py --min-confidence 100", "hadolint --failure-threshold warning Dockerfile"):
        assert tool in block, f"audit-gate does not run: {tool}"


_CI_MAIN = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.mark.skipif(not (ROOT / "docker-compose.dev.yml").exists() or not _CI_MAIN.exists(),
                    reason="host-only static gate — runs in `make ci-tier-0`, not the container lane")
def test_ci_runs_the_audit_gate_as_its_own_job():
    wf = _CI_MAIN.read_text(encoding="utf-8")
    assert "tool-audit:" in wf and "make audit-gate" in wf
    assert "pyright==1.1.414" in wf and "vulture==2.16" in wf and "hadolint/releases/download/v2.15.1" in wf


@pytest.mark.skipif(not (ROOT / "Dockerfile").exists(),
                    reason="host-only static gate — the Dockerfile is not in the test image; runs in `make ci-tier-0`")
def test_dockerfile_user_lines_carry_no_inline_comment():
    """§17.1141 — Docker keeps `# …` on a USER line as PART of the value: the
    image ran as user '10001:10001  # scaffold…' and `docker run` refused it
    (CI unit-tests job, PR #559). Comments go on their own line."""
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    users = [ln for ln in lines if ln.startswith("USER ")]
    assert users, "Dockerfile has no USER instruction"
    for ln in users:
        assert re.fullmatch(r"USER \S+", ln.rstrip()), f"inline comment on a USER line: {ln!r}"


def test_runner_loop_lane_is_throwaway_and_wired_into_tier_2():
    """§17.1153 — the end-to-end runner test runs in the THROWAWAY lane (its
    own engine + helper), never by swapping the live container, and tier 2
    runs it as its last step."""
    src = (ROOT / "Makefile").read_text(encoding="utf-8")
    block = _recipe("test-runner-loop")
    assert "$(_TEST_RUN_PRE)" in block and "scaffold-engine:dev pytest tests/integration/test_runner_loop_live.py" in block
    assert "docker exec $(CONTAINER)" not in block
    assert "-v $(RUNNER_LOOP_LOG_DIR):/itest" in block and "-e ITEST_LOG_DIR=/itest" in block
    assert "test-runner-loop: test-db" in src
    tier2 = _recipe("ci-tier-2")
    assert "step 6/6: local-runner loop end to end" in tier2 and "$(MAKE) test-runner-loop" in tier2
