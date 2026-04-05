"""Tests for Task #1 — Domain filtering in DAG generator.

Tests cover:
  - _normalize_tasks preserves valid domain values
  - _normalize_tasks drops invalid domain values with warning
  - _normalize_tasks treats null/None/"null" domain as absent
  - _normalize_tasks preserves domain on Milvus nodes
  - _normalize_tasks preserves absent domain on non-Milvus nodes
  - validate_dag preserves domain through validation pipeline
  - _enforce_node_count preserves domain on kept nodes
  - VALID_DOMAINS constant has expected values

Run: python3 -m pytest tests/test_domain_filtering.py -v
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Minimal stubs so the module can be imported without app dependencies
# ---------------------------------------------------------------------------
model_router_stub = ModuleType("app.model_router")
model_router_stub.generate = MagicMock()
model_router_stub.settings = MagicMock()
app_stub = ModuleType("app")
app_stub.model_router = model_router_stub
sys.modules.setdefault("app", app_stub)
sys.modules.setdefault("app.model_router", model_router_stub)
sys.modules.setdefault("sqlalchemy", MagicMock())
sys.modules.setdefault("sqlalchemy.ext", MagicMock())
sys.modules.setdefault("sqlalchemy.ext.asyncio", MagicMock())

# Now import via importlib (matches test_validate_dag.py pattern)
from importlib import util as importutil
from pathlib import Path

DAG_GEN_PATH = Path(__file__).resolve().parent.parent / "app" / "modules" / "dag_generator.py"
if not DAG_GEN_PATH.exists():
    DAG_GEN_PATH = Path("app/modules/dag_generator.py")

spec = importutil.spec_from_file_location("dag_generator", DAG_GEN_PATH)
dag_mod = importutil.module_from_spec(spec)
spec.loader.exec_module(dag_mod)

VALID_DOMAINS = dag_mod.VALID_DOMAINS
_normalize_tasks = dag_mod._normalize_tasks
validate_dag = dag_mod.validate_dag
_enforce_node_count = dag_mod._enforce_node_count

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(
    task_id: str = "T1",
    name: str = "Test task",
    task_type: str = "research",
    tool: str = "Milvus",
    domain: object = None,
    depends_on: list | None = None,
    *,
    include_domain: bool = True,
) -> dict:
    """Build a minimal valid task dict."""
    t = {
        "id": task_id,
        "name": name,
        "type": task_type,
        "inputs": [],
        "outputs": [],
        "depends_on": depends_on or [],
        "tool": tool,
    }
    if include_domain:
        t["domain"] = domain
    return t


# ---------------------------------------------------------------------------
# VALID_DOMAINS constant
# ---------------------------------------------------------------------------

class TestValidDomains:
    def test_expected_domains(self):
        assert VALID_DOMAINS == {"prompt", "rag", "eng", "llm", "spec"}

    def test_is_set(self):
        assert isinstance(VALID_DOMAINS, set)


# ---------------------------------------------------------------------------
# _normalize_tasks — domain handling
# ---------------------------------------------------------------------------

class TestNormalizeDomain:
    def test_valid_domain_preserved(self):
        """Valid domain string is kept on the normalized task."""
        for domain in ("prompt", "rag", "eng", "llm", "spec"):
            tasks, errors = _normalize_tasks([_make_task(domain=domain)])
            assert not errors
            assert tasks[0].get("domain") == domain

    def test_invalid_domain_dropped(self):
        """Invalid domain value is silently dropped (logged as warning)."""
        tasks, errors = _normalize_tasks([_make_task(domain="bogus")])
        assert not errors
        assert "domain" not in tasks[0]

    def test_null_string_domain_treated_as_absent(self):
        """Domain set to string 'null' is treated as absent."""
        tasks, errors = _normalize_tasks([_make_task(domain="null")])
        assert not errors
        assert "domain" not in tasks[0]

    def test_none_domain_treated_as_absent(self):
        """Domain set to None is treated as absent."""
        tasks, errors = _normalize_tasks([_make_task(domain=None)])
        assert not errors
        assert "domain" not in tasks[0]

    def test_empty_string_domain_treated_as_absent(self):
        """Domain set to empty string is treated as absent."""
        tasks, errors = _normalize_tasks([_make_task(domain="")])
        assert not errors
        assert "domain" not in tasks[0]

    def test_domain_case_insensitive(self):
        """Domain matching is case-insensitive."""
        tasks, errors = _normalize_tasks([_make_task(domain="RAG")])
        assert not errors
        assert tasks[0].get("domain") == "rag"

    def test_domain_whitespace_stripped(self):
        """Domain value has whitespace stripped."""
        tasks, errors = _normalize_tasks([_make_task(domain="  eng  ")])
        assert not errors
        assert tasks[0].get("domain") == "eng"

    def test_non_milvus_tool_domain_still_preserved(self):
        """Domain is preserved even on non-Milvus tools (LLM might set it)."""
        tasks, errors = _normalize_tasks([_make_task(tool="LLM", domain="llm")])
        assert not errors
        assert tasks[0].get("domain") == "llm"

    def test_no_domain_key_in_input(self):
        """When domain key is absent from input, output has no domain."""
        tasks, errors = _normalize_tasks([_make_task(include_domain=False)])
        assert not errors
        assert "domain" not in tasks[0]

    def test_multiple_tasks_mixed_domains(self):
        """Multiple tasks with different domain states all handled correctly."""
        raw = [
            _make_task("T1", tool="SearXNG", domain=None),
            _make_task("T2", tool="Milvus", domain="rag", depends_on=["T1"]),
            _make_task("T3", tool="LLM", domain="bogus", depends_on=["T2"]),
        ]
        tasks, errors = _normalize_tasks(raw)
        assert not errors
        assert "domain" not in tasks[0]       # None → absent
        assert tasks[1].get("domain") == "rag"  # valid → kept
        assert "domain" not in tasks[2]         # invalid → dropped


# ---------------------------------------------------------------------------
# validate_dag — domain passthrough
# ---------------------------------------------------------------------------

class TestValidateDagDomain:
    def test_domain_survives_validation(self):
        """validate_dag does not strip the domain field."""
        raw = [
            _make_task("T1", tool="SearXNG", domain=None),
            _make_task("T2", tool="Milvus", domain="prompt", depends_on=["T1"]),
        ]
        normalized, _ = _normalize_tasks(raw)
        validated, warnings = validate_dag(normalized)
        assert validated[1].get("domain") == "prompt"

    def test_domain_absent_survives_validation(self):
        """Nodes without domain pass through validate_dag unchanged."""
        raw = [_make_task("T1", tool="LLM", domain=None)]
        normalized, _ = _normalize_tasks(raw)
        validated, _ = validate_dag(normalized)
        assert "domain" not in validated[0]


# ---------------------------------------------------------------------------
# _enforce_node_count — domain passthrough
# ---------------------------------------------------------------------------

class TestEnforceNodeCountDomain:
    def test_domain_preserved_on_kept_nodes(self):
        """When truncating, domain is preserved on kept nodes."""
        raw = [
            _make_task(f"T{i}", domain="eng" if i == 1 else None)
            for i in range(1, 8)
        ]
        normalized, _ = _normalize_tasks(raw)
        result = _enforce_node_count(normalized)
        assert len(result) == 5
        t1 = next(t for t in result if t["id"] == "T1")
        assert t1.get("domain") == "eng"

    def test_domain_preserved_under_count(self):
        """Under-count nodes still have their domain."""
        raw = [_make_task("T1", domain="spec"), _make_task("T2", domain="rag", depends_on=["T1"])]
        normalized, _ = _normalize_tasks(raw)
        result = _enforce_node_count(normalized)
        assert result[0].get("domain") == "spec"
        assert result[1].get("domain") == "rag"
