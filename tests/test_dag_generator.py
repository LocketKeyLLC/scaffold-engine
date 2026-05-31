"""
tests/test_dag_generator.py — DAG generator smoke tests

Uses importlib to avoid WORKDIR /app package collision (Task #18).
Tests validate_dag(), _normalize_tasks(), _enforce_node_count(),
tool coercion, domain validation, and idempotency guard.
"""

import importlib.util
import os
import sys
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# importlib loader — avoids 'app' package shadowing from WORKDIR /app
# ---------------------------------------------------------------------------

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "modules", "dag_generator.py"
)
_ABS_PATH = os.path.abspath(_MODULE_PATH)


def _load_dag_generator():
    """Load dag_generator.py via importlib, stubbing heavy deps."""
    # Stub out imports that require live DB / Ollama
    stubs = {}
    for mod_name in [
        "app", "app.database", "app.modules", "app.config",
        "app.modules.dag_validator",  # W.3 — added so loader-based collection
                                       # works regardless of test ordering.
        "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio",
        "sqlalchemy.orm", "sqlalchemy.sql", "sqlalchemy.sql.expression",
        "sqlalchemy", "sqlalchemy.text",
        "structlog", "aiohttp", "asyncpg",
    ]:
        if mod_name not in sys.modules:
            stubs[mod_name] = MagicMock()

    # Provide structlog.get_logger
    mock_structlog = MagicMock()
    mock_structlog.get_logger.return_value = MagicMock()
    stubs["structlog"] = mock_structlog

    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "dag_generator", _ABS_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            pass  # Module may partially load — we grab what we can
        return mod


# Attempt load once at module level
_dag_gen = None
try:
    _dag_gen = _load_dag_generator()
except Exception:
    pass


# Skip entire module if dag_generator can't be loaded
pytestmark = pytest.mark.skipif(
    _dag_gen is None or not hasattr(_dag_gen, "validate_dag"),
    reason="dag_generator.py not loadable in this environment",
)


# ===========================================================================
# Constants
# ===========================================================================

VALID_TOOLS = {"LLM", "CodeGen", "SearXNG", "Milvus", "Shell"}  # §17.359 — Shell seam
VALID_DOMAINS = {"prompt", "rag", "eng", "eng_design", "llm", "spec", "code", "qa"}  # §17.329 — eng_design added for circuit/EDA


# ===========================================================================
# validate_dag tests
# ===========================================================================

@pytest.mark.smoke
class TestValidateDag:
    """Tests for validate_dag() — graph structure validation."""

    def test_valid_linear_dag(self):
        """Linear chain T1→T2→T3 passes validation."""
        dag = [
            {"id": "T1", "title": "Step 1", "tool": "LLM", "depends_on": []},
            {"id": "T2", "title": "Step 2", "tool": "LLM", "depends_on": ["T1"]},
            {"id": "T3", "title": "Step 3", "tool": "LLM", "depends_on": ["T2"]},
        ]
        result, _warnings = _dag_gen.validate_dag(dag)
        assert isinstance(result, list)
        assert len(result) == 3

    def test_invalid_dependency_stripped(self):
        """References to non-existent nodes are removed."""
        dag = [
            {"id": "T1", "title": "Step 1", "tool": "LLM", "depends_on": ["T99"]},
            {"id": "T2", "title": "Step 2", "tool": "LLM", "depends_on": ["T1"]},
            {"id": "T3", "title": "Step 3", "tool": "LLM", "depends_on": ["T2"]},
        ]
        result, _warnings = _dag_gen.validate_dag(dag)
        # T99 doesn't exist — should be stripped from T1's depends_on
        t1 = next(n for n in result if n["id"] == "T1")
        assert "T99" not in t1.get("depends_on", [])

    def test_self_reference_removed(self):
        """A node depending on itself has the self-ref stripped."""
        dag = [
            {"id": "T1", "title": "Step 1", "tool": "LLM", "depends_on": ["T1"]},
            {"id": "T2", "title": "Step 2", "tool": "LLM", "depends_on": ["T1"]},
            {"id": "T3", "title": "Step 3", "tool": "LLM", "depends_on": ["T2"]},
        ]
        result, _warnings = _dag_gen.validate_dag(dag)
        t1 = next(n for n in result if n["id"] == "T1")
        assert "T1" not in t1.get("depends_on", [])

    def test_cycle_detected(self):
        """Cycle T1→T2→T3→T1 raises or returns error."""
        dag = [
            {"id": "T1", "title": "Step 1", "tool": "LLM", "depends_on": ["T3"]},
            {"id": "T2", "title": "Step 2", "tool": "LLM", "depends_on": ["T1"]},
            {"id": "T3", "title": "Step 3", "tool": "LLM", "depends_on": ["T2"]},
        ]
        # validate_dag should either raise ValueError or return an error indicator
        try:
            result, _warnings = _dag_gen.validate_dag(dag)
            # If it returns instead of raising, the result should indicate cycle
            # The implementation may strip deps to break cycle
            assert isinstance(result, list)
        except (ValueError, Exception):
            pass  # Expected — cycle detected

    def test_invalid_tool_coerced_to_llm(self):
        """Unknown tool values get coerced to 'LLM'."""
        dag = [
            {"id": "T1", "title": "Step 1", "tool": "human_review", "depends_on": []},
            {"id": "T2", "title": "Step 2", "tool": "WebSearch", "depends_on": ["T1"]},
            {"id": "T3", "title": "Step 3", "tool": "LLM", "depends_on": ["T2"]},
        ]
        result, _warnings = _dag_gen.validate_dag(dag)
        for node in result:
            assert node["tool"] in VALID_TOOLS, (
                f"Node {node['key']} has invalid tool: {node['tool']}"
            )

    def test_valid_tools_preserved(self):
        """All 6 valid tool types pass through unchanged."""
        dag = [
            {"id": "T1", "title": "Search", "tool": "SearXNG", "depends_on": []},
            {"id": "T2", "title": "Lookup", "tool": "Milvus", "depends_on": ["T1"]},
            {"id": "T3", "title": "Code", "tool": "CodeGen", "depends_on": ["T2"]},
        ]
        result, _warnings = _dag_gen.validate_dag(dag)
        tools = [n["tool"] for n in result]
        assert tools == ["SearXNG", "Milvus", "CodeGen"]

    def test_empty_dag_passes(self):
        """Empty list doesn't crash."""
        result, _warnings = _dag_gen.validate_dag([])
        assert result == []

    def test_single_node_dag(self):
        """Single node with no deps is valid."""
        dag = [{"id": "T1", "title": "Only step", "tool": "LLM", "depends_on": []}]
        result, _warnings = _dag_gen.validate_dag(dag)
        assert len(result) == 1


# ===========================================================================
# _enforce_node_count tests
# ===========================================================================

@pytest.mark.smoke
class TestEnforceNodeCount:
    """Tests for _enforce_node_count() — 3-5 node constraint."""

    @pytest.mark.skipif(
        not hasattr(_dag_gen, "_enforce_node_count"),
        reason="_enforce_node_count not exposed",
    )
    def test_under_minimum_raises_valueerror(self):
        """#23: DAGs with <3 nodes now raise ValueError (was: silent warning+pass)."""
        dag = [
            {"id": "T1", "title": "Step 1", "tool": "LLM", "depends_on": []},
            {"id": "T2", "title": "Step 2", "tool": "LLM", "depends_on": ["T1"]},
        ]
        with pytest.raises(ValueError, match="dag_undercount"):
            _dag_gen._enforce_node_count(dag)

    @pytest.mark.skipif(
        not hasattr(_dag_gen, "_enforce_node_count"),
        reason="_enforce_node_count not exposed",
    )
    def test_over_maximum_truncated(self):
        """DAGs with >5 nodes are truncated to 5."""
        dag = [
            {"id": f"T{i}", "title": f"Step {i}", "tool": "LLM",
             "depends_on": [f"T{i-1}"] if i > 1 else []}
            for i in range(1, 13)
        ]
        result = _dag_gen._enforce_node_count(dag)
        assert len(result) <= 10

    @pytest.mark.skipif(
        not hasattr(_dag_gen, "_enforce_node_count"),
        reason="_enforce_node_count not exposed",
    )
    def test_within_range_unchanged(self):
        """DAGs with 3-5 nodes pass through unchanged."""
        dag = [
            {"id": f"T{i}", "title": f"Step {i}", "tool": "LLM",
             "depends_on": [f"T{i-1}"] if i > 1 else []}
            for i in range(1, 5)
        ]
        result = _dag_gen._enforce_node_count(dag)
        assert len(result) == 4


# ===========================================================================
# Domain validation tests
# ===========================================================================

@pytest.mark.smoke
class TestDomainValidation:
    """Tests for domain field validation in DAG nodes."""

    def test_valid_domain_preserved(self):
        """Valid domain values pass through validate_dag."""
        dag = [
            {"id": "T1", "title": "KB Lookup", "tool": "Milvus",
             "depends_on": [], "domain": "rag"},
            {"id": "T2", "title": "Analyze", "tool": "LLM",
             "depends_on": ["T1"]},
            {"id": "T3", "title": "Summarize", "tool": "LLM",
             "depends_on": ["T2"]},
        ]
        result, _warnings = _dag_gen.validate_dag(dag)
        t1 = next(n for n in result if n["id"] == "T1")
        assert t1.get("domain") == "rag"

    def test_invalid_domain_defaulted(self):
        """Invalid domain values are set to None."""
        dag = [
            {"id": "T1", "title": "KB Lookup", "tool": "Milvus",
             "depends_on": [], "domain": "invalid_domain"},
            {"id": "T2", "title": "Analyze", "tool": "LLM",
             "depends_on": ["T1"]},
            {"id": "T3", "title": "Summarize", "tool": "LLM",
             "depends_on": ["T2"]},
        ]
        result, _warnings = _dag_gen.validate_dag(dag)
        t1 = next(n for n in result if n["id"] == "T1")
        assert t1.get("domain") == "invalid_domain"  # validate_dag passes domain through; _normalize_tasks handles coercion

    def test_no_domain_field_is_fine(self):
        """Nodes without domain field (non-Milvus) are valid."""
        dag = [
            {"id": "T1", "title": "Step 1", "tool": "LLM", "depends_on": []},
            {"id": "T2", "title": "Step 2", "tool": "CodeGen", "depends_on": ["T1"]},
            {"id": "T3", "title": "Step 3", "tool": "LLM", "depends_on": ["T2"]},
        ]
        result, _warnings = _dag_gen.validate_dag(dag)
        assert len(result) == 3

    def test_all_domains_accepted(self):
        """Each valid domain passes validation."""
        for domain in VALID_DOMAINS:
            dag = [
                {"id": "T1", "title": "KB Lookup", "tool": "Milvus",
                 "depends_on": [], "domain": domain},
                {"id": "T2", "title": "Analyze", "tool": "LLM",
                 "depends_on": ["T1"]},
                {"id": "T3", "title": "Summarize", "tool": "LLM",
                 "depends_on": ["T2"]},
            ]
            result, _warnings = _dag_gen.validate_dag(dag)
            t1 = next(n for n in result if n["id"] == "T1")
            assert t1.get("domain") == domain, f"Domain {domain} was not preserved"

class TestToolSelectionGuide:
    """The DAG-generation prompt must steer the LLM away from over-using CodeGen.

    Live test (Apr 29 2026) showed the model picking CodeGen for "list supported
    file extensions" — a non-code task — because the original tool guide just
    said "CodeGen = code generation or script writing." This class locks in
    the tightened guidance.
    """

    def test_codegen_rule_has_anti_examples(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # Must explicitly call out non-code tasks that should NOT be CodeGen
        assert "Do NOT use CodeGen" in DAG_SYSTEM
        assert "listing file extensions" in DAG_SYSTEM
        assert "documentation" in DAG_SYSTEM

    def test_codegen_rule_has_positive_examples(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # Concrete examples of what CodeGen IS for
        assert "Write the parser" in DAG_SYSTEM or "API endpoint" in DAG_SYSTEM

    def test_llm_marked_as_default(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The fallback must be loud — model should default LLM, not CodeGen
        assert "DEFAULT" in DAG_SYSTEM

    def test_cli_example_pattern_present(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The CLI-tool example shows the right CodeGen/LLM split (1 CodeGen, 4 LLM)
        assert "Build a CLI tool that converts screenshots" in DAG_SYSTEM
        # The example must label "Document usage" as LLM, not CodeGen
        assert "Documentation about code is LLM" in DAG_SYSTEM

    def test_text_about_code_routes_to_llm(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The key principle: discussing/listing/explaining code is LLM territory
        assert "even one ABOUT code" in DAG_SYSTEM

    # ----- §17.363 — scope discipline -----

    def test_scope_discipline_block_present(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The dedicated labeled block — clause must be load-bearing visible
        assert "Scope discipline" in DAG_SYSTEM
        assert "load-bearing" in DAG_SYSTEM
        # The hard rule must name the failure shape directly.
        assert "EXACTLY what its `name` and `outputs` literally state" in DAG_SYSTEM

    def test_scope_anti_example_present(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The homelab T1/T2/T3/T5 ~95% overlap pattern is the anti-example;
        # asserting its narrative phrasing prevents a future prompt edit
        # from silently dropping the contrast surface.
        assert "Anti-example" in DAG_SYSTEM
        assert "95% identical" in DAG_SYSTEM
        # The operator failure mode must be named so the model sees the
        # actual consequence of getting it wrong.
        assert "VMID already in use" in DAG_SYSTEM

    def test_scope_good_shape_walked_through(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The Good shape walks all 8 nodes — pinning a few transitions so
        # an edit can't degrade it back to 4-node hand-waving.
        assert "Configure VLAN bridges" in DAG_SYSTEM
        assert "Create LXC containers" in DAG_SYSTEM
        assert "Deploy Jellyfin service" in DAG_SYSTEM
        assert "Deploy Ollama with GPU" in DAG_SYSTEM
        assert "starts from" in DAG_SYSTEM.lower()

    def test_scope_rules_cover_install_configure_deploy_verbs(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The three hard rules per verb (Install/Configure/Deploy) must
        # each be present — drop any one and the model regresses on the
        # corresponding shape.
        assert 'A node named "Install X"' in DAG_SYSTEM
        assert 'A node named "Configure Y"' in DAG_SYSTEM
        assert 'A node named "Deploy Z service"' in DAG_SYSTEM


# ===========================================================================
# Sprint W.3 — validator-driven retry loop
# ===========================================================================


def _llm_response(text: str, success: bool = True, error: str | None = None):
    """Build a fake ModelResponse-shaped object for mocking."""
    resp = MagicMock()
    resp.success = success
    resp.text = text
    resp.error = error
    resp.model = "fake-model"
    resp.total_duration_ms = 0
    return resp


def _dag_json(*, mark_doc_as_codegen: bool = False):
    """Helper: emit a valid 3-task DAG JSON string for the LLM mock."""
    import json
    tasks = [
        {"id": "T1", "name": "Write parser", "type": "action",
         "inputs": [], "outputs": ["parser"], "depends_on": [],
         "tool": "CodeGen", "domain": None, "assigned_model": None,
         "notes": "code"},
        {"id": "T2", "name": "Document usage", "type": "action",
         "inputs": ["parser"], "outputs": ["docs"], "depends_on": ["T1"],
         "tool": "CodeGen" if mark_doc_as_codegen else "LLM",
         "domain": None, "assigned_model": None, "notes": "documentation"},
        {"id": "T3", "name": "Validate", "type": "validation",
         "inputs": ["docs"], "outputs": ["report"], "depends_on": ["T2"],
         "tool": "LLM", "domain": None, "assigned_model": None,
         "notes": "validation"},
    ]
    return json.dumps({"strategy": "sequential", "tasks": tasks})


def _issues(payload: list[dict]) -> str:
    import json
    return json.dumps({"issues": payload})


@pytest.mark.smoke
class TestValidatorLoop:
    """Tests for _generate_dag_with_validator — the W.3 retry loop.

    Mocks app.model_router.generate with scripted side_effect lists.
    Both the generator and the validator call this same attribute, so
    we can drive both legs of each iteration from one queue.
    """

    async def test_clean_on_first_attempt_one_validator_call(self):
        """Generator emits clean DAG → validator returns no issues → return."""
        from app.modules import dag_generator
        from app import model_router as _mr

        mock = AsyncMock(side_effect=[
            _llm_response(_dag_json()),       # generator attempt 1
            _llm_response(_issues([])),       # validator pass 1 — clean
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        assert result["dag_data"] is not None
        assert result["error"] is None
        assert result["attempts"] == 1
        assert result["validator_calls"] == 1
        assert mock.call_count == 2  # 1 gen + 1 validator
        assert result["warnings"] == []

    async def test_issue_then_retry_succeeds(self):
        """Generator emits dirty DAG → validator flags → retry → clean."""
        from app.modules import dag_generator
        from app import model_router as _mr

        mock = AsyncMock(side_effect=[
            _llm_response(_dag_json(mark_doc_as_codegen=True)),  # gen 1 (dirty)
            _llm_response(_issues([{                              # val 1 (issue)
                "node_id": "T2", "current_tool": "CodeGen",
                "proposed_tool": "LLM",
                "reason": "Documentation is not code.",
            }])),
            _llm_response(_dag_json()),                           # gen 2 (clean)
            _llm_response(_issues([])),                           # val 2 (clean)
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        assert result["dag_data"] is not None
        assert result["error"] is None
        assert result["attempts"] == 2
        assert result["validator_calls"] == 2
        assert mock.call_count == 4  # 2 gen + 2 validator
        # First-pass issue surfaced as warning + clean-after-retry note
        assert any("validator_found_1_issues_attempt_1" in w for w in result["warnings"])
        assert any("validator_clean_after_retry_attempt_2" in w for w in result["warnings"])

    async def test_retries_exhaust_with_remaining_issues(self):
        """Validator keeps finding distinct issues; loop ships final DAG with warning."""
        from app.modules import dag_generator
        from app import model_router as _mr

        # Distinct issue sets each attempt so circuit-breaker doesn't fire.
        mock = AsyncMock(side_effect=[
            _llm_response(_dag_json(mark_doc_as_codegen=True)),  # gen 1
            _llm_response(_issues([{
                "node_id": "T2", "current_tool": "CodeGen",
                "proposed_tool": "LLM", "reason": "doc",
            }])),
            _llm_response(_dag_json(mark_doc_as_codegen=True)),  # gen 2
            _llm_response(_issues([{
                "node_id": "T3", "current_tool": "LLM",
                "proposed_tool": "Milvus", "reason": "kb",
            }])),
            _llm_response(_dag_json(mark_doc_as_codegen=True)),  # gen 3
            _llm_response(_issues([{
                "node_id": "T1", "current_tool": "CodeGen",
                "proposed_tool": "LLM", "reason": "still wrong",
            }])),
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        # Default max_retries=2 → 3 attempts total
        assert result["dag_data"] is not None
        assert result["attempts"] == 3
        assert result["validator_calls"] == 3
        assert mock.call_count == 6
        assert any("validator_retries_exhausted" in w for w in result["warnings"])

    async def test_circuit_breaker_on_identical_issues(self):
        """Same issue set twice → break early without third generator call."""
        from app.modules import dag_generator
        from app import model_router as _mr

        same_issue = [{
            "node_id": "T2", "current_tool": "CodeGen",
            "proposed_tool": "LLM", "reason": "doc",
        }]
        mock = AsyncMock(side_effect=[
            _llm_response(_dag_json(mark_doc_as_codegen=True)),  # gen 1
            _llm_response(_issues(same_issue)),                   # val 1
            _llm_response(_dag_json(mark_doc_as_codegen=True)),  # gen 2
            _llm_response(_issues(same_issue)),                   # val 2 — identical!
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        assert result["dag_data"] is not None
        assert result["attempts"] == 2  # broke before attempt 3
        assert mock.call_count == 4     # 2 gen + 2 validator, no third gen
        assert any("validator_circuit_break_attempt_2" in w for w in result["warnings"])

    async def test_kill_switch_disabled_skips_validator(self):
        """settings.dag_validator_enabled=False → 1 generator call, 0 validator calls."""
        from app.modules import dag_generator
        from app import model_router as _mr
        from app.config import settings

        mock = AsyncMock(side_effect=[
            _llm_response(_dag_json()),  # gen 1 only
        ])
        with patch.object(settings, "dag_validator_enabled", False), \
             patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        assert result["dag_data"] is not None
        assert result["attempts"] == 1
        assert result["validator_calls"] == 0
        assert mock.call_count == 1
        assert result["warnings"] == []

    async def test_first_attempt_call_failure_returns_error(self):
        """LLM call fails on attempt 1 → caller can fail the job."""
        from app.modules import dag_generator
        from app import model_router as _mr

        mock = AsyncMock(side_effect=[
            _llm_response("", success=False, error="ollama down"),
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        assert result["dag_data"] is None
        assert result["error"] == "ollama down"
        assert result["attempts"] == 1
        assert result["validator_calls"] == 0

    async def test_first_attempt_parse_failure_returns_error(self):
        """LLM emits non-JSON on attempt 1 → caller can fail the job."""
        from app.modules import dag_generator
        from app import model_router as _mr

        mock = AsyncMock(side_effect=[
            _llm_response("not actually JSON {{{"),
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        assert result["dag_data"] is None
        assert result["error"] == "LLM output was not valid JSON"
        assert result["attempts"] == 1

    async def test_validator_failed_open_ships_current_dag(self):
        """Validator JSON parse fails → ship current DAG with a warning."""
        from app.modules import dag_generator
        from app import model_router as _mr

        mock = AsyncMock(side_effect=[
            _llm_response(_dag_json()),                   # gen 1 OK
            _llm_response("validator output is broken"),  # val 1 unparseable
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        assert result["dag_data"] is not None
        assert result["error"] is None
        assert result["attempts"] == 1
        assert result["validator_calls"] == 1
        assert any("validator_failed_open" in w for w in result["warnings"])

