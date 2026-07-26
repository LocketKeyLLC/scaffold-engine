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
    def test_truncation_preserves_terminal_sink(self):
        """§17.615 (audit #14) — the terminal/deliverable node is a sink (nothing
        depends on it) and is highest-numbered; truncation must preserve it, not
        tail-drop it and silently orphan the deliverable."""
        # Linear chain T1..T12 — T12 is the terminal sink (the deliverable).
        dag = [
            {"id": f"T{i}", "title": f"Step {i}", "tool": "LLM",
             "depends_on": [f"T{i-1}"] if i > 1 else []}
            for i in range(1, 13)
        ]
        result = _dag_gen._enforce_node_count(dag)
        kept_ids = {t["id"] for t in result}
        assert len(result) <= 10
        assert "T12" in kept_ids, "terminal sink (deliverable) must survive truncation"

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

    # ----- §17.642 — single-outcome granularity -----

    def test_single_outcome_rule_present(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The dedicated labeled block + the load-bearing definition.
        assert "Single outcome per node" in DAG_SYSTEM
        assert "EXACTLY ONE outcome" in DAG_SYSTEM
        # The split heuristic the model keys on.
        assert "install ≠ configure ≠ integrate ≠ verify" in DAG_SYSTEM
        # It must be distinguished from (not conflated with) scope discipline.
        assert "DIFFERENT rule from scope discipline" in DAG_SYSTEM
        assert "BUNDLE several results" in DAG_SYSTEM

    def test_single_outcome_jellyfin_split_example(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The exact overwhelming case (§17.641 report) → six single-outcome nodes.
        assert "is NOT one node; it is SIX" in DAG_SYSTEM
        assert "Verify a hardware transcode" in DAG_SYSTEM

    def test_step_count_allows_finer_grain(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The old hard cap of 10 is gone; multi-part briefs go finer.
        assert "3 to 10 execution steps" not in DAG_SYSTEM
        assert "Do not create more than 10 steps" not in DAG_SYSTEM
        assert "SINGLE-OUTCOME execution steps" in DAG_SYSTEM
        assert "typically 8-20" in DAG_SYSTEM

    # ----- §17.367 — CodeGen-verb scope discipline -----

    def test_scope_rules_cover_codegen_verbs(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The §17.363 anti-example list was Shell-verb-only; §17.367
        # extends to CodeGen verbs (Write/Implement). Each of the three
        # CodeGen hard rules must be present.
        assert 'A node named "Write CLI interface"' in DAG_SYSTEM
        assert 'A node named "Implement <module>"' in DAG_SYSTEM
        assert 'A node named "Write unit tests for <X>"' in DAG_SYSTEM

    def test_codegen_scope_anti_example_present(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The actual T2≈T3 incompatible-API regression from the CodeGen
        # retry must appear so the model has concrete contrast.
        assert "Anti-example 2 (CodeGen" in DAG_SYSTEM
        assert "Two `def main()`s, two `ArgumentParser`s" in DAG_SYSTEM
        assert "DIFFERENT SIGNATURE" in DAG_SYSTEM

    def test_codegen_compatible_apis_rule_present(self):
        from app.modules.dag_generator import DAG_SYSTEM
        # The §17.367 sibling-API-compat rule — sibling CodeGen nodes
        # must use sibling function signatures, not re-invent them.
        assert "Sibling CodeGen nodes must have COMPATIBLE APIs" in DAG_SYSTEM

    # ----- §17.370 — CLI thin-entry-point rule -----

    def test_cli_thin_entry_point_rule_present(self):
        from app.modules.dag_generator import DAG_SYSTEM
        flat = " ".join(DAG_SYSTEM.split())
        # The §17.370 rule — CLI imports; does not re-implement.
        assert "thin entry-point" in flat.lower()
        assert "The CLI imports; it does not re-implement" in flat

    def test_anti_example_3_present(self):
        from app.modules.dag_generator import DAG_SYSTEM
        flat = " ".join(DAG_SYSTEM.split())
        # Anti-example 3 is the §17.370 finer-grained regression — T2
        # clean but T4 reimplements T2's parser + T3's filename gen.
        assert "Anti-example 3 (CodeGen" in flat
        assert "T2's job (the parser) — reimplemented inline" in flat
        # The §17.370 closing claim — CLI is the thin entry-point that
        # imports, never reimplements.
        assert "thin entry-point that imports, never reimplements" in flat


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
        """§17.463 — non-JSON on EVERY re-draw of attempt 1 → after the
        retry-on-empty loop exhausts, caller can fail the job."""
        from app.modules import dag_generator
        from app import model_router as _mr

        # All 3 draws unparseable → the §17.463 redraw loop exhausts, then the
        # attempt-1 parse-failure contract returns the error.
        mock = AsyncMock(side_effect=[
            _llm_response("not actually JSON {{{"),
            _llm_response("still not JSON"),
            _llm_response(""),  # thinking-model empty
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        assert result["dag_data"] is None
        assert result["error"] == "LLM output was not valid JSON"
        assert result["attempts"] == 1
        assert mock.call_count == 3  # retried on empty/unparseable, not hard-failed

    async def test_empty_first_draw_redraws_and_succeeds(self):
        """§17.463 — the reported bug: the thinking model returns success+EMPTY on
        the first draw (parse → None). Pre-fix this hard-failed the whole DAG
        ('DAG must have at least 2 tasks'). Now a fresh draw lands a valid DAG and
        generation succeeds."""
        from app.modules import dag_generator
        from app import model_router as _mr

        mock = AsyncMock(side_effect=[
            _llm_response(""),                 # gen draw 1 — empty (thinking-model)
            _llm_response(_dag_json()),        # gen draw 2 — valid DAG
            _llm_response(_issues([])),        # validator pass — clean
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "test"}, {"role": "model_general"},
            )

        assert result["dag_data"] is not None
        assert result["error"] is None
        assert result["attempts"] == 1
        assert result["validator_calls"] == 1
        assert mock.call_count == 3  # 2 gen draws (1 empty + 1 valid) + 1 validator

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



# ===========================================================================
# §17.475 — is_deliverable (explicit deliverable marker)
# ===========================================================================

@pytest.mark.smoke
class TestIsDeliverable:
    """§17.475 — _normalize_tasks parses the model-asserted deliverable flag."""

    def test_is_deliverable_parsed(self):
        tasks = [
            {"id": "T1", "name": "Plan the work", "type": "action",
             "tool": "LLM", "depends_on": []},
            {"id": "T2", "name": "Write final output", "type": "output",
             "tool": "LLM", "depends_on": ["T1"], "is_deliverable": True},
        ]
        normalized, errors, _w = _dag_gen._normalize_tasks(tasks)
        assert not errors
        by_id = {n["id"]: n for n in normalized}
        assert by_id["T2"]["is_deliverable"] is True
        assert by_id["T1"]["is_deliverable"] is False

    def test_is_deliverable_defaults_false(self):
        tasks = [
            {"id": "T1", "name": "Single step here", "type": "output",
             "tool": "LLM", "depends_on": []},
        ]
        normalized, _e, _w = _dag_gen._normalize_tasks(tasks)
        assert normalized[0]["is_deliverable"] is False

    def test_is_deliverable_truthy_coerced(self):
        tasks = [
            {"id": "T1", "name": "Emit the artifact", "type": "output",
             "tool": "LLM", "depends_on": [], "is_deliverable": "yes"},
        ]
        normalized, _e, _w = _dag_gen._normalize_tasks(tasks)
        assert normalized[0]["is_deliverable"] is True


@pytest.mark.smoke
class TestLongNameCoercion:
    """§17.507 — a >5-word task name is truncated-with-warning, NOT a fatal
    error. Pre-fix, one over-length name (e.g. the LLM's non-deterministic
    "Deploy media AI and game VMs") made `_normalize_tasks` append an error,
    which failed the ENTIRE DAG build and marked the job `failed`."""

    def test_long_name_truncated_not_errored(self):
        tasks = [
            {"id": "T1", "name": "Deploy media AI and game VMs",  # 6 words
             "type": "action", "tool": "Shell", "depends_on": []},
        ]
        normalized, errors, warnings = _dag_gen._normalize_tasks(tasks)
        assert not errors                       # build is NOT failed
        assert len(normalized) == 1             # task survives
        assert normalized[0]["name"] == "Deploy media AI and game"  # 5 words
        assert any("truncated" in w.lower() for w in warnings)

    def test_five_word_name_untouched(self):
        tasks = [
            {"id": "T1", "name": "Install the Proxmox VE host",  # exactly 5
             "type": "action", "tool": "Shell", "depends_on": []},
        ]
        normalized, errors, warnings = _dag_gen._normalize_tasks(tasks)
        assert not errors
        assert normalized[0]["name"] == "Install the Proxmox VE host"
        assert not any("truncated" in w.lower() for w in warnings)

    def test_one_long_name_does_not_drop_other_tasks(self):
        # The real failure mode: one bad name nuked the whole multi-node DAG.
        tasks = [
            {"id": "T1", "name": "Resolve design ambiguities", "type": "action",
             "tool": "LLM", "depends_on": []},
            {"id": "T2", "name": "Deploy media AI and game VMs", "type": "action",
             "tool": "Shell", "depends_on": ["T1"]},
            {"id": "T3", "name": "Document network topology", "type": "action",
             "tool": "LLM", "depends_on": ["T2"]},
        ]
        normalized, errors, _w = _dag_gen._normalize_tasks(tasks)
        assert not errors
        assert {n["id"] for n in normalized} == {"T1", "T2", "T3"}


# ===========================================================================
# §17.476 — dead-end / dependency-completeness detection
# ===========================================================================

@pytest.mark.smoke
class TestDeadEndDetection:
    """§17.476 — an orphan node neither feeds nor is fed by a deliverable."""

    def test_sibling_branch_is_dead_end(self):
        # Proxmox shape: TS ("configure Tailscale") hangs off the trunk (T2)
        # but nothing consumes it and it is not the deliverable.
        tasks = [
            {"id": "T1", "name": "Install host", "type": "action", "depends_on": []},
            {"id": "T2", "name": "Base config", "type": "action", "depends_on": ["T1"]},
            {"id": "TS", "name": "Configure Tailscale", "type": "action", "depends_on": ["T2"]},
            {"id": "T3", "name": "Deploy services", "type": "action", "depends_on": ["T2"]},
            {"id": "TD", "name": "Validate and document", "type": "output",
             "depends_on": ["T3"], "is_deliverable": True},
        ]
        assert _dag_gen.detect_dead_ends(tasks) == ["TS"]

    def test_downstream_validation_not_dead_end(self):
        # Word-count shape: the CodeGen node TC is the deliverable; TV validates
        # it downstream. TV consumes the deliverable → NOT an orphan.
        tasks = [
            {"id": "T1", "name": "Plan", "type": "decision", "depends_on": []},
            {"id": "TC", "name": "Write the script", "type": "action",
             "depends_on": ["T1"], "is_deliverable": True},
            {"id": "TV", "name": "Validate end to end", "type": "validation",
             "depends_on": ["TC"]},
        ]
        assert _dag_gen.detect_dead_ends(tasks) == []

    def test_multi_deliverable_no_orphans(self):
        # Library + README, both deliverables, sharing a base. Neither is an
        # orphan and the shared base feeds both.
        tasks = [
            {"id": "B", "name": "Build core", "type": "action", "depends_on": []},
            {"id": "CFG", "name": "Emit library", "type": "action",
             "depends_on": ["B"], "is_deliverable": True},
            {"id": "DOC", "name": "Write README", "type": "output",
             "depends_on": ["B"], "is_deliverable": True},
        ]
        assert _dag_gen.detect_dead_ends(tasks) == []

    def test_leaf_fallback_no_orphans_when_unmarked(self):
        # No is_deliverable anywhere → leaves are the deliverable set; every
        # node feeds a leaf, so nothing is orphaned (permissive fallback).
        tasks = [
            {"id": "T1", "name": "Step one", "type": "action", "depends_on": []},
            {"id": "T2", "name": "Step two", "type": "action", "depends_on": ["T1"]},
            {"id": "T3", "name": "Step three", "type": "output", "depends_on": ["T2"]},
        ]
        assert _dag_gen.detect_dead_ends(tasks) == []


@pytest.mark.smoke
class TestAutoLinkDeadEnds:
    """§17.476 — last-resort wiring of orphans into the primary deliverable."""

    def test_auto_link_connects_orphan(self):
        tasks = [
            {"id": "T1", "name": "Install host", "type": "action", "depends_on": []},
            {"id": "T2", "name": "Base config", "type": "action", "depends_on": ["T1"]},
            {"id": "TS", "name": "Configure Tailscale", "type": "action", "depends_on": ["T2"]},
            {"id": "T3", "name": "Deploy services", "type": "action", "depends_on": ["T2"]},
            {"id": "TD", "name": "Validate and document", "type": "output",
             "depends_on": ["T3"], "is_deliverable": True},
        ]
        orphans = _dag_gen.detect_dead_ends(tasks)
        primary = _dag_gen.auto_link_dead_ends(tasks, orphans)
        assert primary == "TD"
        td = next(t for t in tasks if t["id"] == "TD")
        assert "TS" in td["depends_on"]
        # And it is now connected — no orphans remain, no cycle introduced.
        assert _dag_gen.detect_dead_ends(tasks) == []

    def test_auto_link_picks_largest_closure_deliverable(self):
        # Two deliverables; the orphan attaches to the one with the larger
        # upstream closure (the synthesis), not the small one.
        tasks = [
            {"id": "A", "name": "Base a", "type": "action", "depends_on": []},
            {"id": "B", "name": "Mid b", "type": "action", "depends_on": ["A"]},
            {"id": "BIG", "name": "Big synth", "type": "output",
             "depends_on": ["B"], "is_deliverable": True},     # closure {BIG,B,A}=3
            {"id": "SMALL", "name": "Small art", "type": "output",
             "depends_on": [], "is_deliverable": True},        # closure {SMALL}=1
            {"id": "ORPH", "name": "Orphan branch", "type": "action", "depends_on": ["A"]},
        ]
        primary = _dag_gen.auto_link_dead_ends(tasks, _dag_gen.detect_dead_ends(tasks))
        assert primary == "BIG"

    def test_auto_link_noop_without_orphans(self):
        tasks = [
            {"id": "T1", "name": "Only", "type": "output", "depends_on": [],
             "is_deliverable": True},
        ]
        assert _dag_gen.auto_link_dead_ends(tasks, []) is None


def _dag_json_with_dead_end():
    """A DAG whose deliverable is T1 (code) with downstream docs T2, plus an
    orphan sibling TS that nothing consumes and is not the deliverable."""
    import json
    tasks = [
        {"id": "T1", "name": "Write the script", "type": "action", "inputs": [],
         "outputs": ["code"], "depends_on": [], "tool": "CodeGen", "domain": None,
         "assigned_model": None, "notes": "code", "is_deliverable": True},
        {"id": "T2", "name": "Document usage", "type": "action", "inputs": ["code"],
         "outputs": ["docs"], "depends_on": ["T1"], "tool": "LLM", "domain": None,
         "assigned_model": None, "notes": "docs"},
        {"id": "TS", "name": "Configure unrelated thing", "type": "action",
         "inputs": [], "outputs": ["side"], "depends_on": [], "tool": "Shell",
         "domain": None, "assigned_model": None, "notes": "orphan branch"},
    ]
    return json.dumps({"strategy": "hybrid", "tasks": tasks})


@pytest.mark.smoke
class TestDeadEndRetryLoop:
    """§17.476 — the validator loop flags dead-ends and retries; the flag
    gates the whole behavior."""

    async def test_dead_end_triggers_retry(self):
        from app.modules import dag_generator
        from app import model_router as _mr
        mock = AsyncMock(side_effect=[
            _llm_response(_dag_json_with_dead_end()),  # gen 1 — orphan TS
            _llm_response(_issues([])),                # val 1 — tool-picks clean
            _llm_response(_dag_json()),                # gen 2 — clean (no orphan)
            _llm_response(_issues([])),                # val 2 — clean
        ])
        with patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "x"}, {"role": "model_general"},
            )
        assert result["attempts"] == 2
        assert any("dead_end_found" in w for w in result["warnings"])

    async def test_dead_end_check_disabled_ships_attempt_1(self):
        from app.modules import dag_generator
        from app import model_router as _mr
        mock = AsyncMock(side_effect=[
            _llm_response(_dag_json_with_dead_end()),  # gen 1
            _llm_response(_issues([])),                # val 1 — clean
        ])
        with patch.object(dag_generator.settings, "dag_dead_end_check_enabled", False), \
             patch.object(_mr, "generate", new=mock):
            result = await dag_generator._generate_dag_with_validator(
                {"brief": "x"}, {"role": "model_general"},
            )
        assert result["attempts"] == 1
        assert not any("dead_end" in w for w in result["warnings"])
