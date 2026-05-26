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
# #9.14 note: The real _check_milvus is a closure inside app.main.health(),
# so it cannot be imported and called directly. A true behavioral test would
# need to exercise the full /health endpoint with a live Milvus container.
# For unit tests, we parse app/main.py via AST and inspect the function body
# as a structured tree (not a regex over source text). This catches the same
# regressions the original grep tests did, but via AST traversal.
class TestMilvusHealthBehavior:
    """AST inspection of the _check_milvus closure body."""

    @pytest.fixture(scope="class")
    def check_milvus_fn(self):
        """Return the ast.AsyncFunctionDef node for _check_milvus."""
        import ast
        main_path = Path(__file__).resolve().parent.parent / "app" / "main.py"
        tree = ast.parse(main_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_check_milvus":
                return node
        pytest.fail("_check_milvus function not found in app/main.py")

    def test_no_flush_call(self, check_milvus_fn):
        """Body must not contain any .flush() attribute access."""
        import ast
        for node in ast.walk(check_milvus_fn):
            if isinstance(node, ast.Attribute) and node.attr == "flush":
                pytest.fail(f"flush attribute access found at line {node.lineno}")

    def test_reads_num_entities(self, check_milvus_fn):
        """Body must read .num_entities."""
        import ast
        found = any(
            isinstance(node, ast.Attribute) and node.attr == "num_entities"
            for node in ast.walk(check_milvus_fn)
        )
        assert found, "num_entities attribute access not found"

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
        """Some string literal must parameterize confidence via :conf."""
        matches = [s for s in sql_literals if "SET confidence = :conf" in s]
        assert matches, "No SQL literal uses parameterized ':conf' for confidence"


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

          1. An ARG line has ``MODEL_RERANKER=tomaarsen/Qwen3-Reranker-0.6B-seq-cls``
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
        arg_steps = [args for op, args in instructions if op == "ARG"]
        canonical_arg = "MODEL_RERANKER=tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
        assert any(canonical_arg in a for a in arg_steps), (
            f"No ARG line sets {canonical_arg!r}; "
            f"saw ARGs: {arg_steps!r}"
        )
        run_steps = [args for op, args in instructions if op == "RUN"]
        download_refs_model = any(
            "${MODEL_RERANKER}" in r or "Qwen3-Reranker-0.6B-seq-cls" in r
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

    def test_triggers_on_push_to_main(self, workflow):
        # YAML 'on:' key is parsed by pyyaml as boolean True for bare 'on'.
        # Handle both stringified and bool key.
        triggers = workflow.get("on") or workflow.get(True)
        assert triggers is not None, "workflow has no 'on:' section"
        push = triggers.get("push", {}) if isinstance(triggers, dict) else {}
        branches = push.get("branches", []) if isinstance(push, dict) else []
        assert "main" in branches, f"push.branches doesn't include main: {branches}"

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

    def test_ignores_eval_retrieval_script(self, workflow):
        """The retrieval eval script is excluded from CI (needs Milvus+Ollama)."""
        # This check is inherently about "the exclusion string appears somewhere"
        # — structurally it's in an addopts/--ignore flag inside a run command.
        jobs = workflow.get("jobs", {})
        all_runs = []
        for job in jobs.values():
            for step in job.get("steps", []):
                if isinstance(step, dict) and step.get("run"):
                    all_runs.append(step["run"])
        combined = "\n".join(all_runs)
        assert "eval_retrieval.py" in combined, (
            "eval_retrieval.py not referenced in any CI run command"
        )
