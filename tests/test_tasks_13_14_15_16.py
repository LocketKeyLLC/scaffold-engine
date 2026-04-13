"""Tests for Task #13 (Milvus health latency) and Task #15 (confidence scoring).

Uses importlib.util to avoid WORKDIR /app collision (Task #18).
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Import helpers (importlib.util pattern) ──────────────────────────


def _load_module(name: str, rel_path: str):
    """Load a module by file path, avoiding 'from app.modules...' collision."""
    base = Path(__file__).resolve().parent.parent / "app"
    spec = importlib.util.spec_from_file_location(name, base / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Load status.py for response model tests ─────────────────────────

_status_path = Path(__file__).resolve().parent.parent / "app" / "routers" / "status.py"
_status_spec = importlib.util.spec_from_file_location("status_mod", _status_path)
_status_mod = importlib.util.module_from_spec(_status_spec)
# Don't exec — just grab source for AST/text-based tests
_status_source = _status_path.read_text()


# =====================================================================
# Task #13 — Milvus Health Latency
# =====================================================================

_main_path = Path(__file__).resolve().parent.parent / "app" / "main.py"
_main_source = _main_path.read_text()


class TestMilvusHealthNoFlush:
    """Verify that _check_milvus does NOT call collection.flush()."""

    def test_no_flush_in_health_check(self):
        """main.py _check_milvus must not contain col.flush()."""
        assert "_check_milvus" in _main_source, "_check_milvus not found in main.py"

        start = _main_source.index("async def _check_milvus")
        lines = _main_source[start:].split("\n")
        func_body = []
        for i, line in enumerate(lines):
            if i == 0:
                func_body.append(line)
                continue
            if line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                break
            func_body.append(line)

        func_text = "\n".join(func_body)
        assert "flush()" not in func_text, (
            "col.flush() still present in _check_milvus — Task #13 not applied"
        )

    def test_num_entities_still_queried(self):
        """Health check should still read num_entities for entry count."""
        start = _main_source.index("async def _check_milvus")
        end = _main_source.index("pg, ollama, milvus = await asyncio.gather")
        func_text = _main_source[start:end]
        assert "num_entities" in func_text, (
            "num_entities query missing from _check_milvus"
        )

    def test_collection_name_unchanged(self):
        """Should still check 'toon_v2' collection."""
        assert '"toon_v2"' in _main_source


# =====================================================================
# Task #15 — Confidence Scoring
# =====================================================================


class TestConfidenceInSchema:
    """Verify confidence column and field plumbing."""

    def test_init_sql_has_confidence_column(self):
        """db/init.sql must declare confidence column in dag_nodes."""
        init_path = Path(__file__).resolve().parent.parent / "db" / "init.sql"
        if not init_path.exists():
            pytest.skip("db/init.sql not available inside container")
        init_sql = init_path.read_text()
        assert "confidence" in init_sql, "confidence column missing from init.sql"
        assert "FLOAT" in init_sql.upper().split("confidence")[1][:30].upper()

    def test_migration_file_exists(self):
        """Migration 002 must exist for running databases."""
        migration = (
            Path(__file__).resolve().parent.parent
            / "db"
            / "migrations"
            / "002_add_confidence.sql"
        )
        if not migration.exists():
            pytest.skip("db/migrations/ not available inside container")
        content = migration.read_text()
        assert "ALTER TABLE dag_nodes ADD COLUMN confidence" in content

    def test_nodelog_model_has_confidence(self):
        """NodeLog Pydantic model must include confidence field."""
        assert "confidence" in _status_source
        assert "Optional[float]" in _status_source or "float" in _status_source

    def test_logs_query_selects_confidence(self):
        """The /logs SQL query must SELECT the confidence column."""
        assert "confidence" in _status_source.split("def get_logs")[1].split("FROM dag_nodes")[0], (
            "confidence not in /logs SELECT clause"
        )

    def test_nodelog_construction_includes_confidence(self):
        """NodeLog construction must pass confidence=row.confidence."""
        after_get_logs = _status_source.split("def get_logs")[1]
        assert "confidence=row.confidence" in after_get_logs, (
            "NodeLog construction missing confidence=row.confidence"
        )


class TestConfidenceInExecutionAgent:
    """Verify execution_agent stores verifier confidence."""

    _ea_path = Path(__file__).resolve().parent.parent / "app" / "modules" / "execution_agent.py"
    _ea_source = _ea_path.read_text()

    def test_no_null_hardcode(self):
        """Should not hardcode confidence = NULL anymore."""
        assert "SET confidence = NULL" not in self._ea_source, (
            "Still hardcoding confidence = NULL — Task #15 not applied"
        )

    def test_stores_confidence_param(self):
        """Should use a parameterized confidence value."""
        assert "SET confidence = :conf" in self._ea_source, (
            "Expected parameterized confidence update"
        )

    def test_todo_removed(self):
        """The old TODO comment should be gone."""
        assert "TODO: Populate confidence via logprob" not in self._ea_source


# =====================================================================
# Task #16 — Reranker Pre-Download
# =====================================================================


class TestDockerfileReranker:
    """Verify Dockerfile pre-downloads reranker weights."""

    _df_path = Path(__file__).resolve().parent.parent / "Dockerfile"

    @pytest.fixture(autouse=True)
    def _require_dockerfile(self):
        if not self._df_path.exists():
            pytest.skip("Dockerfile not available inside container")
        self._dockerfile = self._df_path.read_text()

    def test_snapshot_download_present(self):
        """Dockerfile must include a RUN step to download the reranker."""
        assert "snapshot_download" in self._dockerfile

    def test_correct_model_name(self):
        """Must download tomaarsen/Qwen3-Reranker-0.6B-seq-cls."""
        assert "Qwen3-Reranker-0.6B-seq-cls" in self._dockerfile

    def test_cache_dir_matches_compose(self):
        """Cache dir must match HF_HOME in docker-compose.yml."""
        assert "/app/.cache/huggingface" in self._dockerfile

    def test_download_before_copy(self):
        """Download step must come AFTER pip install (deps needed)
        and BEFORE COPY app/ (so weights are cached in layer)."""
        lines = self._dockerfile.split("\n")
        pip_line = next(i for i, l in enumerate(lines) if "requirements.txt" in l and "pip install" in l)
        dl_line = next(i for i, l in enumerate(lines) if "snapshot_download" in l)
        copy_line = next(i for i, l in enumerate(lines) if l.startswith("COPY app/"))
        assert pip_line < dl_line < copy_line, (
            f"Order wrong: pip={pip_line}, download={dl_line}, copy={copy_line}"
        )


# =====================================================================
# Task #14 — CI/CD Pipeline
# =====================================================================


class TestCIWorkflow:
    """Verify GitHub Actions workflow file."""

    _wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test.yml"

    @pytest.fixture(autouse=True)
    def _require_workflow(self):
        if not self._wf_path.exists():
            pytest.skip(".github/workflows/test.yml not available inside container")
        self._content = self._wf_path.read_text()

    def test_triggers_on_push_to_main(self):
        assert "push:" in self._content
        assert "main" in self._content

    def test_postgres_service(self):
        assert "postgres:16" in self._content

    def test_runs_pytest(self):
        assert "pytest" in self._content

    def test_ignores_eval_script(self):
        assert "eval_retrieval.py" in self._content
