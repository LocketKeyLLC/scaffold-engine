"""Infrastructure scaffolding tests.

Covers cross-cutting build/deploy invariants that don't fit in any single
module's test file:
  - Milvus health check latency (no forced flushes)
  - Confidence scoring fields in schema + execution_agent
  - Dockerfile pre-download of reranker weights
  - CI workflow structure

#9.7  - Renamed from test_tasks_13_14_15_16.py (legacy task numbering).
#9.14 - Source-grep tests converted to behavioral mocks (Task #13/#15) and
        structured parsing (Task #14 YAML, Task #16 Dockerfile) where
        feasible; file-artifact checks (migration SQL existence, init.sql
        column presence) remain as file checks because that's the right
        abstraction for them.
"""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =====================================================================
# Task #13 - Milvus Health Latency (AST-based source inspection)
# =====================================================================
# #9.14 note: The real _check_milvus is a closure inside
# app.health.build_health_response() (moved from app.main in §17.855), so it
# cannot be imported and called directly. A true behavioral test would need to
# exercise the full /health endpoint with a live Milvus container.
# For unit tests, we parse app/main.py via AST and inspect the function body
# as a structured tree (not a regex over source text). This catches the same
# regressions the original grep tests did, but via AST traversal.
class TestMilvusHealthBehavior:
    """AST inspection of the _check_milvus closure body."""

    @pytest.fixture(scope="class")
    def check_milvus_fn(self):
        """Return the ast.AsyncFunctionDef node for _check_milvus.

        §17.855 — the /health probe body (incl. the _check_milvus closure) moved
        from app/main.py to app/health.py::build_health_response; ast.walk still
        recurses into the nested closure there."""
        import ast
        health_path = Path(__file__).resolve().parent.parent / "app" / "health.py"
        tree = ast.parse(health_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_check_milvus":
                return node
        pytest.fail("_check_milvus function not found in app/health.py")

    def test_no_flush_call(self, check_milvus_fn):
        """Body must not contain any .flush() attribute access."""
        import ast
        for node in ast.walk(check_milvus_fn):
            if isinstance(node, ast.Attribute) and node.attr == "flush":
                pytest.fail(f"flush attribute access found at line {node.lineno}")

    def test_reads_row_count(self, check_milvus_fn):
        """Body must read the entry count via MilvusClient.get_collection_stats
        (§17.591 — replaced the ORM ``Collection.num_entities`` property)."""
        import ast
        found = any(
            isinstance(node, ast.Attribute) and node.attr == "get_collection_stats"
            for node in ast.walk(check_milvus_fn)
        )
        assert found, "get_collection_stats attribute access not found"

    def test_targets_toon_v2_collection(self, check_milvus_fn):
        """Body must reference the literal string 'toon_v2'."""
        import ast
        literals = [
            node.value for node in ast.walk(check_milvus_fn)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert "toon_v2" in literals, f"'toon_v2' literal not found; got: {literals}"


# =====================================================================
# Task #15 - Confidence in Schema (mix of behavioral + file-artifact)
# =====================================================================
class TestConfidenceInSchema:
    """NodeLog schema + init.sql + migration file."""

    def test_init_sql_declares_confidence_column(self):
        """db/init.sql declares confidence column typed as FLOAT.

        Kept as a file-artifact check: init.sql is the artifact that
        matters; there's no behavioral path to exercise without a live DB.
        """
        init_path = Path(__file__).resolve().parent.parent / "db" / "init.sql"
        if not init_path.exists():
            pytest.skip("db/init.sql not available inside container")
        init_sql = init_path.read_text()
        upper = init_sql.upper()
        assert "CONFIDENCE" in upper
        assert "FLOAT" in upper.split("CONFIDENCE", 1)[1][:30]

    def test_migration_002_exists_and_alters_table(self):
        """Migration 002 is present and adds the confidence column."""
        migration = (
            Path(__file__).resolve().parent.parent
            / "db" / "migrations" / "002_add_confidence.sql"
        )
        if not migration.exists():
            pytest.skip("db/migrations/ not available inside container")
        content = migration.read_text()
        assert "ALTER TABLE dag_nodes ADD COLUMN confidence" in content

    def test_nodelog_pydantic_model_has_confidence_field(self):
        """Behavioral: import NodeLog and assert field declared on the model."""
        from app.routers.status import NodeLog
        # Pydantic v2: .model_fields; v1: .__fields__
        fields = getattr(NodeLog, "model_fields", None) or NodeLog.__fields__
        assert "confidence" in fields, (
            f"NodeLog missing 'confidence' field. Fields present: {list(fields)}"
        )

    def test_nodelog_confidence_is_optional_float(self):
        """Behavioral: NodeLog(confidence=None) and NodeLog(confidence=0.9) both construct."""
        from app.routers.status import NodeLog
        # Build minimum args — ignore other required fields by letting pydantic default
        try:
            instance_none = NodeLog(
                node_key="T1", title="x", tool="LLM", status="done",
                output_preview="", confidence=None,
            )
            instance_set = NodeLog(
                node_key="T1", title="x", tool="LLM", status="done",
                output_preview="", confidence=0.9,
            )
        except Exception as e:
            pytest.fail(f"NodeLog construction failed with confidence: {e}")
        assert instance_none.confidence is None
        assert instance_set.confidence == 0.9


# =====================================================================
# Task #15 - Confidence in execution_agent SQL (AST-based)
# =====================================================================
class TestConfidenceInExecutionAgent:
    """Check the SQL string literal inside execution_agent via AST inspection."""

    @pytest.fixture(scope="class")
    def sql_literals(self):
        """Collect all string literals from execution_agent.py via AST."""
        import ast
        ea_path = (
            Path(__file__).resolve().parent.parent
            / "app" / "modules" / "execution_agent.py"
        )
        tree = ast.parse(ea_path.read_text())
        literals = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value)
        return literals

    def test_no_hardcoded_null_confidence_sql(self, sql_literals):
        """No string literal contains 'SET confidence = NULL'."""
        offenders = [s for s in sql_literals if "SET confidence = NULL" in s]
        assert not offenders, (
            f"Hardcoded NULL confidence found in: {offenders[:1]}"
        )

    def test_confidence_is_bound_as_parameter(self, sql_literals):
        """Confidence must be written via a bound parameter, never interpolated.

        §17.407 folded the confidence write into ``_set_node_status``: the
        literal changed from a standalone ``SET confidence = :conf`` to a CASE
        guard binding ``:confidence`` (with a CAST for asyncpg NULL type
        inference). The invariant under test is unchanged — confidence is
        parameterized, not f-string interpolated — so accept either shape.
        """
        matches = [
            s for s in sql_literals
            if "SET confidence = :conf" in s
            or ("confidence = CASE" in s and ":confidence" in s)
        ]
        assert matches, (
            "No SQL literal binds confidence as a parameter "
            "(expected ':conf' or a CASE guard with ':confidence')"
        )


# =====================================================================
# Task #16 - Dockerfile reranker pre-download (structured parse)
# =====================================================================
class TestDockerfileReranker:
    """Parse Dockerfile as structured instruction stream, not raw text."""

    _df_path = Path(__file__).resolve().parent.parent / "Dockerfile"

    @pytest.fixture(autouse=True)
    def _require_dockerfile(self):
        if not self._df_path.exists():
            pytest.skip("Dockerfile not available inside container")

    @pytest.fixture
    def instructions(self):
        """Return a list of (instruction, args) tuples, skipping comments/empty."""
        lines = self._df_path.read_text().splitlines()
        # Join continuation lines (backslash-newline)
        joined = []
        buf = ""
        for line in lines:
            stripped = line.rstrip()
            if stripped.endswith("\\"):
                buf += stripped[:-1] + " "
                continue
            buf += stripped
            if buf.strip() and not buf.lstrip().startswith("#"):
                parts = buf.strip().split(None, 1)
                if len(parts) == 2:
                    joined.append((parts[0].upper(), parts[1]))
                elif len(parts) == 1:
                    joined.append((parts[0].upper(), ""))
            buf = ""
        return joined

    def test_has_snapshot_download_run_step(self, instructions):
        run_steps = [args for op, args in instructions if op == "RUN"]
        assert any("snapshot_download" in r for r in run_steps), (
            "No RUN step invokes snapshot_download"
        )

    def test_downloads_correct_reranker_model(self, instructions):
        """§17.324 — the canonical model name lives in `ARG MODEL_RERANKER`
        and the RUN step references it via ``${MODEL_RERANKER}``. The
        prior version of this test only scanned RUN args and failed
        post-parameterization. Pin both halves of the contract:

          1. An ARG line has ``MODEL_RERANKER=<settings.model_reranker default>``
          2. The snapshot_download RUN step references ``${MODEL_RERANKER}``
             (or the literal name, for backward-compat with future un-
             parameterized rewrites — either form is correct as long as
             #1 holds).

        Together these catch: a Dockerfile that swaps the default to a
        different model (fails #1); a Dockerfile that parameterizes but
        then RUN-hardcodes a stale literal (fails #2 because the literal
        wouldn't equal the ARG); a parameterized Dockerfile pointing at
        the canonical model (passes both).
        """
        # §17.1129 — the canonical name is the config default (one source of
        # truth, kept aligned with the Dockerfile ARG + .env.example by
        # `make check-rerank-drift`); a literal here went stale at §17.1124.
        from app.config import Settings
        canonical_model = Settings.model_fields["model_reranker"].default
        arg_steps = [args for op, args in instructions if op == "ARG"]
        canonical_arg = f"MODEL_RERANKER={canonical_model}"
        assert any(canonical_arg in a for a in arg_steps), (
            f"No ARG line sets {canonical_arg!r}; "
            f"saw ARGs: {arg_steps!r}"
        )
        run_steps = [args for op, args in instructions if op == "RUN"]
        download_refs_model = any(
            "${MODEL_RERANKER}" in r or canonical_model.split("/")[-1] in r
            for r in run_steps
        )
        assert download_refs_model, (
            "snapshot_download RUN step does not reference ${MODEL_RERANKER} "
            "or the literal canonical model name"
        )

    def test_cache_dir_matches_compose(self, instructions):
        """HF cache dir in Dockerfile should match the compose HF_HOME mount."""
        all_text = " ".join(f"{op} {args}" for op, args in instructions)
        assert "/code/.cache/huggingface" in all_text

    def test_download_step_ordered_between_pip_install_and_app_copy(self, instructions):
        """Pip install, then snapshot_download, then COPY of app sources."""
        pip_idx = next(
            i for i, (op, args) in enumerate(instructions)
            if op == "RUN" and "pip install" in args and "requirements.txt" in args
        )
        dl_idx = next(
            i for i, (op, args) in enumerate(instructions)
            if op == "RUN" and "snapshot_download" in args
        )
        copy_idx = next(
            i for i, (op, args) in enumerate(instructions)
            if op == "COPY" and re.search(r"\bapp[/\s]", args)
        )
        assert pip_idx < dl_idx < copy_idx, (
            f"Ordering wrong: pip={pip_idx}, download={dl_idx}, copy={copy_idx}"
        )


# =====================================================================
# Task #14 - CI Workflow (YAML parsing, not substring grep)
# =====================================================================
class TestCIWorkflow:
    """Parse test.yml as YAML and assert on structured fields."""

    _wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test.yml"

    @pytest.fixture(autouse=True)
    def _require_workflow(self):
        if not self._wf_path.exists():
            pytest.skip(".github/workflows/test.yml not available inside container")

    @pytest.fixture
    def workflow(self):
        import yaml
        return yaml.safe_load(self._wf_path.read_text())

    @staticmethod
    def _triggers(wf):
        # YAML 'on:' is parsed by pyyaml as boolean True for the bare key.
        t = wf.get("on") or wf.get(True)
        assert isinstance(t, dict), "workflow has no 'on:' section"
        return t

    def test_push_is_not_pinned_to_main(self, workflow):
        """§17.1179 — a pushed branch must run its gates BEFORE a PR exists.

        This asserted `"main" in push.branches`, which is exactly the shape that
        left six branches of the §17.1171–1178 arc with zero runs each: work sat
        unverified until someone opened a PR, and five of those six were merged
        having never been built as units. The billing reason §17.902 gave for the
        pin ("a metered private repo") stopped being true when this repo went
        public at v1.2.0. `push:` must therefore carry no `branches` filter.
        """
        push = self._triggers(workflow).get("push") or {}
        assert "branches" not in push, (
            "push is pinned to specific branches, so a feature-branch push runs "
            f"nothing until a PR exists: {push.get('branches')}"
        )

    def test_pull_request_is_not_filtered_by_base(self, workflow):
        """§17.902 + §17.1179 — `pull_request: branches: [main]` filters on the
        PR's BASE, so a STACKED PR (opened against another feature branch) gets
        no checks at all. §17.902 found this, fixed ci.yml, and left the
        byte-identical filter in test.yml — so every stacked PR in this repo has
        had one workflow's gates and not the other's."""
        pr = self._triggers(workflow).get("pull_request") or {}
        assert "branches" not in pr, (
            f"pull_request is filtered by base branch: {pr.get('branches')}"
        )

    def test_both_workflows_declare_the_same_triggers(self):
        """The two gating workflows must arm on the same events. They drifted
        once already (§17.902 fixed one copy of two); this is the assertion that
        makes the next divergence fail instead of going unnoticed for months."""
        import yaml
        ci = self._wf_path.parent / "ci.yml"
        if not ci.exists():
            pytest.skip("ci.yml not available inside container")
        mine = self._triggers(yaml.safe_load(self._wf_path.read_text()))
        theirs = self._triggers(yaml.safe_load(ci.read_text()))
        for event in ("push", "pull_request"):
            assert (mine.get(event) or {}) == (theirs.get(event) or {}), (
                f"test.yml and ci.yml disagree on the '{event}' trigger: "
                f"{mine.get(event)!r} vs {theirs.get(event)!r}"
            )

    def test_the_goldens_nightly_always_reports(self):
        """§17.1180b (audit I5) — an absent quality signal must not look like a
        quiet one.

        The job carried `if: vars.SCAFFOLD_SELF_HOSTED == '1'`, so with no
        self-hosted runner registered every nightly completed as **skipped** in
        6-9 s and said nothing. Measured: 8 of 8 scheduled runs over 19-26 Sep.
        That is §17.1170's silent-branch shape applied to the only automated
        quality gate this engine has.

        The job must therefore run unconditionally and explain itself when it
        cannot measure. It must NOT fail — a permanently red nightly is the
        §17.1169 lesson, and "not measured" is not "regressed".
        """
        import yaml
        wf = self._wf_path.parent / "goldens.yml"
        if not wf.exists():
            pytest.skip("goldens.yml not available inside container")
        doc = yaml.safe_load(wf.read_text())
        job = (doc.get("jobs") or {}).get("goldens")
        assert job, "the goldens job is gone"
        assert "if" not in job, (
            "a job-level `if:` makes the nightly complete as 'skipped' with no "
            "output — the absence of the signal becomes invisible"
        )
        steps = job.get("steps") or []
        assert all("if" in st for st in steps), (
            "every step must be guarded, or `make goldens` runs on a GitHub "
            "runner that has no Ollama"
        )
        report = [st for st in steps
                  if "GITHUB_STEP_SUMMARY" in str(st.get("run", ""))]
        assert len(report) == 1, "exactly one step must report the missing run"
        body = str(report[0].get("run"))
        assert "::warning" in body, "the skip needs an annotation, not only a summary"
        assert "make goldens" in body, "the report must name the manual fallback"
        assert report[0].get("if", "").find("!=") != -1, (
            "the report step must run on the NO-runner path"
        )

    def test_the_goldens_job_name_distinguishes_measured_from_not_measured(self):
        """A green tick on a run that measured nothing is worse than a skip.
        The job name carries the distinction into the checks list."""
        import yaml
        wf = self._wf_path.parent / "goldens.yml"
        if not wf.exists():
            pytest.skip("goldens.yml not available inside container")
        name = str(((yaml.safe_load(wf.read_text()).get("jobs") or {})
                    .get("goldens") or {}).get("name", ""))
        assert "SCAFFOLD_SELF_HOSTED" in name and "NOT RUN" in name.upper(), (
            f"the job name must say when nothing was measured: {name!r}"
        )

    def test_the_self_hosted_tier_stays_pinned_to_main(self):
        """Widening the push trigger must NOT put the heavy Docker stack on the
        self-hosted runner for every feature branch. Tier 2 carries its own
        `if:`; this asserts that guard is what keeps it off."""
        import yaml
        ci = self._wf_path.parent / "ci.yml"
        if not ci.exists():
            pytest.skip("ci.yml not available inside container")
        jobs = (yaml.safe_load(ci.read_text()) or {}).get("jobs", {})
        selfhosted = {k: v for k, v in jobs.items()
                      if "self-hosted" in str(v.get("runs-on", ""))}
        assert selfhosted, "no self-hosted job found — has Tier 2 moved?"
        for name, job in selfhosted.items():
            cond = str(job.get("if", ""))
            assert "refs/heads/main" in cond and "push" in cond, (
                f"self-hosted job {name!r} is not pinned to main+push: {cond!r}"
            )

    def test_has_postgres_service(self, workflow):
        """Some job should declare a postgres:16 service container."""
        jobs = workflow.get("jobs", {})
        found = False
        for job in jobs.values():
            services = job.get("services", {}) or {}
            for svc in services.values():
                if isinstance(svc, dict) and "postgres" in str(svc.get("image", "")):
                    assert "16" in str(svc["image"]), (
                        f"Postgres service uses wrong version: {svc['image']}"
                    )
                    found = True
        assert found, "No job declares a postgres service"

    def test_runs_pytest_command(self, workflow):
        """Some step in some job invokes pytest."""
        jobs = workflow.get("jobs", {})
        found = False
        for job in jobs.values():
            for step in job.get("steps", []):
                run = step.get("run", "") if isinstance(step, dict) else ""
                if "pytest" in run:
                    found = True
                    break
            if found:
                break
        assert found, "No workflow step invokes pytest"
