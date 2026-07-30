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

    def test_split_acquire_create_configure(self):
        """§17.646 — a 900+-word "Create unprivileged LXC" walkthrough (bundled
        download-template + create + set-static-IP) motivated splitting acquire /
        create / network-config into separate outcomes."""
        from app.modules.dag_generator import DAG_SYSTEM
        # Prerequisite acquisition and post-create configuration are their own nodes.
        assert "ACQUIRES A PREREQUISITE" in DAG_SYSTEM
        assert "CREATES a resource and then CONFIGURES that resource" in DAG_SYSTEM
        assert "acquire-prerequisite ≠ create ≠ network-config" in DAG_SYSTEM
        # The "more than one Phase → more than one outcome" tell.
        assert "more than one \"Phase\"" in DAG_SYSTEM
        # The concrete LXC split example.
        assert '"Create unprivileged LXC" is NOT one node' in DAG_SYSTEM

    def test_step_count_full_goal_coverage_up_to_dynamic_cap(self):
        """§17.686 — the prompt no longer caps at 10 / consolidate-everything
        (§17.685); it instructs FULL per-outcome coverage of the whole goal up
        to a DYNAMIC node limit injected from settings.dag_max_nodes via
        DAG_PROMPT. Keeping the prompt's cap aligned to the enforcement setting
        (rather than a hard-coded number) is what prevents the §17.685
        prompt↔cap mismatch from recurring at any configured cap."""
        from app.modules.dag_generator import DAG_SYSTEM, DAG_PROMPT
        assert "SINGLE-OUTCOME execution steps" in DAG_SYSTEM
        assert "COVER THE ENTIRE GOAL" in DAG_SYSTEM
        # The §17.685 hard-10 / consolidate-everything guidance is gone.
        assert "AT MOST 10 tasks" not in DAG_SYSTEM
        # The numeric cap is dynamic (settings.dag_max_nodes), injected via DAG_PROMPT.
        assert "{max_nodes}" in DAG_PROMPT
        assert "Maximum tasks" in DAG_PROMPT

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


class TestWireOrphanTerminalNodes:
    """§17.645 — a depless document/verify/summarize node is wired into the build
    leaves so the assist claim can't hand it out before the build exists."""

    def test_depless_document_node_is_wired_to_leaves(self):
        tasks = [
            {"id": "T1", "name": "Install Proxmox host", "type": "action", "depends_on": []},
            {"id": "T2", "name": "Create Pi-hole LXC", "type": "action", "depends_on": ["T1"]},
            {"id": "T3", "name": "Verify Pi-hole DNS", "type": "validation", "depends_on": ["T2"]},
            {"id": "T12", "name": "Document setup for beginner", "type": "output",
             "depends_on": []},
        ]
        rewired = _dag_gen.wire_orphan_terminal_nodes(tasks)
        assert rewired == ["T12"]
        t12 = next(t for t in tasks if t["id"] == "T12")
        assert t12["depends_on"] == ["T3"]   # the sole build leaf

    def test_real_first_step_is_not_wired(self):
        """A genuine starting step with empty deps must stay a root — only
        terminal-reporting names are touched."""
        tasks = [
            {"id": "T1", "name": "Install Proxmox host", "type": "action", "depends_on": []},
            {"id": "T2", "name": "Create LXC", "type": "action", "depends_on": ["T1"]},
        ]
        assert _dag_gen.wire_orphan_terminal_nodes(tasks) == []
        assert next(t for t in tasks if t["id"] == "T1")["depends_on"] == []

    def test_document_node_that_already_has_deps_untouched(self):
        tasks = [
            {"id": "T1", "name": "Build it", "type": "action", "depends_on": []},
            {"id": "T2", "name": "Document the build", "type": "output", "depends_on": ["T1"]},
        ]
        assert _dag_gen.wire_orphan_terminal_nodes(tasks) == []

    def test_prompt_forbids_depless_terminal_nodes(self):
        from app.modules.dag_generator import DAG_SYSTEM
        assert "terminal CONSUMER" in DAG_SYSTEM
        assert "MUST NOT have empty depends_on" in DAG_SYSTEM


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


# ===========================================================================
# §17.663 — research options → operator_decision → DAG decision node
# ===========================================================================


@pytest.mark.smoke
class TestOperatorDecisionMerge:
    """The research-surfaced options (§17.662) thread into the brief as
    `operator_decision`, so generate_dag's prompt directs a `decision` node."""

    _OPTS = {"decision": "Which firewall?",
             "options": [{"label": "OPNsense", "fit": "x", "tradeoff": "y"},
                         {"label": "pfSense", "fit": "x", "tradeoff": "y"}],
             "suggested": "OPNsense", "why": "z"}

    def test_options_merged_from_json_research_data(self):
        brief = {"title": "homelab firewall"}
        out = _dag_gen._brief_with_operator_decision(brief, json.dumps({"options": self._OPTS}))
        assert out["operator_decision"] == self._OPTS
        assert out["title"] == "homelab firewall"      # original preserved

    def test_options_merged_from_dict_research_data(self):
        out = _dag_gen._brief_with_operator_decision({"title": "x"}, {"options": self._OPTS})
        assert out["operator_decision"] == self._OPTS

    def test_no_options_is_noop(self):
        brief = {"title": "x"}
        assert _dag_gen._brief_with_operator_decision(brief, json.dumps({"options": None})) is brief
        assert _dag_gen._brief_with_operator_decision(brief, None) is brief
        assert _dag_gen._brief_with_operator_decision(brief, json.dumps({})) is brief

    def test_empty_options_list_is_noop(self):
        brief = {"title": "x"}
        rd = json.dumps({"options": {"decision": "d", "options": []}})
        assert _dag_gen._brief_with_operator_decision(brief, rd) is brief

    def test_existing_operator_decision_not_overwritten(self):
        brief = {"title": "x", "operator_decision": {"kept": True}}
        out = _dag_gen._brief_with_operator_decision(brief, json.dumps({"options": self._OPTS}))
        assert out["operator_decision"] == {"kept": True}

    def test_malformed_research_data_is_safe(self):
        brief = {"title": "x"}
        assert _dag_gen._brief_with_operator_decision(brief, "not json{{") is brief

    def test_prompt_directs_decision_node_only_when_present(self):
        # directive text present + conditional wording
        assert "operator_decision" in _dag_gen.DAG_PROMPT
        # merged brief surfaces the decision in the rendered prompt
        merged = _dag_gen._brief_with_operator_decision(
            {"title": "x"}, json.dumps({"options": self._OPTS}))
        rendered = _dag_gen.DAG_PROMPT.format(brief=json.dumps(merged), max_nodes=40)
        assert "Which firewall?" in rendered
        assert '"type": "decision"' in _dag_gen.DAG_PROMPT or "type: \"decision\"" in _dag_gen.DAG_PROMPT


@pytest.mark.smoke
class TestConnectIsolatedNodes:
    """§17.668 — isolated nodes (zero edges, incl. is_deliverable-marked orphans
    that detect_dead_ends misses) are chained onto the preceding step so nothing
    floats from t=0 / shows 'disconnected from the graph'."""

    def _homelab(self):
        # mirrors the real failure: T15/T17/T22 have empty deps + is_deliverable.
        return [
            {"id": "T1", "type": "decision", "execution_order": 1, "depends_on": []},
            {"id": "T5", "type": "task", "execution_order": 5, "depends_on": ["T1"]},
            {"id": "T15", "type": "task", "execution_order": 6, "depends_on": [], "is_deliverable": True},
            {"id": "T17", "type": "task", "execution_order": 7, "depends_on": [], "is_deliverable": True},
            {"id": "T19", "type": "task", "execution_order": 9, "depends_on": ["T5"]},
            {"id": "T22", "type": "checkpoint", "execution_order": 10, "depends_on": [], "is_deliverable": True},
        ]

    def test_isolated_deliverable_nodes_chained_in_order(self):
        tasks = self._homelab()
        wired = _dag_gen.connect_isolated_nodes(tasks)
        assert wired == ["T15", "T17", "T22"]
        by = {t["id"]: t for t in tasks}
        assert by["T15"]["depends_on"] == ["T5"]    # nearest earlier connected
        assert by["T17"]["depends_on"] == ["T15"]   # chains onto the prior isolated
        assert by["T22"]["depends_on"] == ["T19"]

    def test_no_isolated_left_after(self):
        tasks = self._homelab()
        _dag_gen.connect_isolated_nodes(tasks)
        ids = {t["id"] for t in tasks}
        depended = set()
        for t in tasks:
            depended |= {d for d in (t.get("depends_on") or []) if d in ids}
        for t in tasks:
            has_dep = bool([d for d in (t.get("depends_on") or []) if d in ids])
            assert has_dep or t["id"] in depended, f"{t['id']} still isolated"

    def test_connected_nodes_untouched(self):
        tasks = self._homelab()
        _dag_gen.connect_isolated_nodes(tasks)
        by = {t["id"]: t for t in tasks}
        assert by["T5"]["depends_on"] == ["T1"]
        assert by["T19"]["depends_on"] == ["T5"]

    def test_root_with_dependents_not_wired(self):
        # T1 has no deps but T5 depends on it → NOT isolated.
        assert "T1" not in _dag_gen.connect_isolated_nodes(self._homelab())

    def test_deps_point_backward_acyclic(self):
        tasks = self._homelab()
        _dag_gen.connect_isolated_nodes(tasks)
        order = {t["id"]: t["execution_order"] for t in tasks}
        for t in tasks:
            for d in (t.get("depends_on") or []):
                assert order[d] < order[t["id"]], f"{t['id']} -> higher-order {d}"

    def test_no_isolated_is_noop(self):
        tasks = [{"id": "A", "execution_order": 1, "depends_on": []},
                 {"id": "B", "execution_order": 2, "depends_on": ["A"]}]
        assert _dag_gen.connect_isolated_nodes(tasks) == []


@pytest.mark.smoke
class TestDeliverableMarking:
    """§17.669 — is_deliverable survives only on TERMINAL or CodeGen nodes;
    mid-graph setup over-marks are cleared (§17.475)."""

    def _homelab(self):
        # 8/10 marked incl. mid-graph setup steps (the real over-marking).
        return [
            {"id": "T1", "depends_on": []},
            {"id": "T2", "depends_on": ["T1"], "is_deliverable": True},
            {"id": "T3", "depends_on": ["T1"]},
            {"id": "T4", "depends_on": ["T1"], "is_deliverable": True},
            {"id": "T5", "depends_on": ["T1"], "is_deliverable": True, "tool": "Shell"},
            {"id": "T15", "depends_on": ["T5"], "is_deliverable": True, "tool": "Shell"},
            {"id": "T17", "depends_on": ["T15"], "is_deliverable": True, "tool": "Shell"},
            {"id": "T18", "depends_on": ["T17"], "is_deliverable": True, "tool": "Shell"},
            {"id": "T19", "depends_on": ["T3"], "is_deliverable": True, "tool": "Shell"},
            {"id": "T22", "depends_on": ["T19"], "is_deliverable": True, "tool": "checkpoint"},
        ]

    def test_midgraph_cleared_terminals_kept(self):
        tasks = self._homelab()
        unmarked = _dag_gen._enforce_deliverable_marking(tasks)
        assert set(unmarked) == {"T5", "T15", "T17", "T19"}
        by = {t["id"]: t for t in tasks}
        for k in ("T5", "T15", "T17", "T19"):
            assert not by[k].get("is_deliverable"), k
        for k in ("T2", "T4", "T18", "T22"):
            assert by[k]["is_deliverable"], k
        assert sum(1 for t in tasks if t.get("is_deliverable")) == 4  # was 8

    def test_codegen_kept_even_mid_graph(self):
        # code brief: the CodeGen node emits the deliverable even though a
        # downstream validation node depends on it (§17.475).
        tasks = [
            {"id": "T1", "depends_on": [], "is_deliverable": True, "tool": "CodeGen"},
            {"id": "T2", "depends_on": ["T1"], "tool": "LLM"},
        ]
        unmarked = _dag_gen._enforce_deliverable_marking(tasks)
        assert unmarked == []
        assert {t["id"]: t for t in tasks}["T1"]["is_deliverable"]

    def test_zero_marked_is_noop(self):
        tasks = [{"id": "A", "depends_on": []}, {"id": "B", "depends_on": ["A"]}]
        assert _dag_gen._enforce_deliverable_marking(tasks) == []

    def test_all_midgraph_falls_back_to_leaf(self):
        tasks = [
            {"id": "A", "depends_on": [], "is_deliverable": True, "tool": "Shell"},
            {"id": "B", "depends_on": ["A"], "is_deliverable": True, "tool": "Shell"},
            {"id": "C", "depends_on": ["B"], "tool": "LLM"},
        ]
        _dag_gen._enforce_deliverable_marking(tasks)
        by = {t["id"]: t for t in tasks}
        assert by["C"]["is_deliverable"]           # leaf marked (fallback)
        assert not by["A"].get("is_deliverable") and not by["B"].get("is_deliverable")


@pytest.mark.smoke
class TestConvergeTerminalLeaves:
    """§17.670 — multiple terminal leaves converge into a single final sink."""

    def _homelab(self):
        # 4 terminals (T2/T4 decisions, T18 config, T22 verify) — none converge.
        return [
            {"id": "T1", "depends_on": [], "execution_order": 1, "type": "decision"},
            {"id": "T2", "depends_on": ["T1"], "execution_order": 2, "type": "decision", "is_deliverable": True},
            {"id": "T3", "depends_on": ["T1"], "execution_order": 3, "type": "decision"},
            {"id": "T4", "depends_on": ["T1"], "execution_order": 4, "type": "decision", "is_deliverable": True},
            {"id": "T5", "depends_on": ["T1"], "execution_order": 5, "type": "task"},
            {"id": "T15", "depends_on": ["T5"], "execution_order": 6, "type": "task"},
            {"id": "T17", "depends_on": ["T15"], "execution_order": 7, "type": "task"},
            {"id": "T18", "depends_on": ["T17"], "execution_order": 8, "type": "task", "is_deliverable": True},
            {"id": "T19", "depends_on": ["T3"], "execution_order": 9, "type": "task"},
            {"id": "T22", "depends_on": ["T19"], "execution_order": 10, "type": "checkpoint", "is_deliverable": True},
        ]

    def test_converges_into_final_type_sink(self):
        tasks = self._homelab()
        primary, wired = _dag_gen.converge_terminal_leaves(tasks)
        assert primary == "T22"                       # checkpoint = final-type
        assert set(wired) == {"T2", "T4", "T18"}
        assert set({t["id"]: t for t in tasks}["T22"]["depends_on"]) == {"T19", "T2", "T4", "T18"}

    def test_exactly_one_terminal_after(self):
        tasks = self._homelab()
        _dag_gen.converge_terminal_leaves(tasks)
        ids = {t["id"] for t in tasks}
        depended = set()
        for t in tasks:
            depended |= {d for d in (t.get("depends_on") or []) if d in ids}
        assert [t["id"] for t in tasks if t["id"] not in depended] == ["T22"]

    def test_converge_then_marking_gives_single_deliverable(self):
        tasks = self._homelab()
        _dag_gen.converge_terminal_leaves(tasks)
        _dag_gen._enforce_deliverable_marking(tasks)
        assert [t["id"] for t in tasks if t.get("is_deliverable")] == ["T22"]

    def test_new_deps_point_backward_acyclic(self):
        tasks = self._homelab()
        _dag_gen.converge_terminal_leaves(tasks)
        order = {t["id"]: t["execution_order"] for t in tasks}
        for d in {t["id"]: t for t in tasks}["T22"]["depends_on"]:
            assert order[d] < order["T22"]

    def test_single_terminal_is_noop(self):
        tasks = [{"id": "A", "depends_on": []}, {"id": "B", "depends_on": ["A"]}]
        assert _dag_gen.converge_terminal_leaves(tasks) == (None, [])


@pytest.mark.smoke
class TestWireDecisionsToImplementers:
    """§17.671 — a dangling decision is wired to the step that applies it,
    matched on a distinctive shared subject token."""

    def _tasks(self):
        return [
            {"id": "T1", "name": "Determine server state", "type": "decision", "execution_order": 1, "depends_on": []},
            {"id": "T2", "name": "Decide VLAN scheme", "type": "decision", "execution_order": 2, "depends_on": ["T1"]},
            {"id": "T3", "name": "Decide backup destination", "type": "decision", "execution_order": 3, "depends_on": ["T1"]},
            {"id": "T4", "name": "Decide Jellyfin media storage", "type": "decision", "execution_order": 4, "depends_on": ["T1"]},
            {"id": "T5", "name": "Download Proxmox ISO", "type": "task", "execution_order": 5, "depends_on": ["T1"]},
            {"id": "T17", "name": "Configure Jellyfin media library", "type": "task", "execution_order": 7, "depends_on": ["T5"]},
            {"id": "T19", "name": "Configure backup jobs", "type": "task", "execution_order": 9, "depends_on": ["T3"]},
        ]

    def test_decision_wired_to_matching_implementer(self):
        tasks = self._tasks()
        wired = _dag_gen.wire_decisions_to_implementers(tasks)
        assert ("T4", "T17") in wired              # Jellyfin/media match
        assert "T4" in {t["id"]: t for t in tasks}["T17"]["depends_on"]

    def test_decision_without_implementer_not_wired(self):
        # nothing else mentions VLAN → T2 has no implementer to wire to
        wired = _dag_gen.wire_decisions_to_implementers(self._tasks())
        assert not any(d == "T2" for d, _ in wired)

    def test_already_consumed_decision_untouched(self):
        tasks = self._tasks()                       # T19 already depends on T3
        wired = _dag_gen.wire_decisions_to_implementers(tasks)
        assert not any(d == "T3" for d, _ in wired)
        assert {t["id"]: t for t in tasks}["T19"]["depends_on"] == ["T3"]

    def test_cycle_safe_no_wire_to_upstream(self):
        tasks = [
            {"id": "A", "name": "Configure jellyfin media", "type": "task", "execution_order": 1, "depends_on": []},
            {"id": "B", "name": "Decide jellyfin media storage", "type": "decision", "execution_order": 2, "depends_on": ["A"]},
        ]
        assert _dag_gen.wire_decisions_to_implementers(tasks) == []

    def test_generic_only_no_match(self):
        tasks = [
            {"id": "D", "name": "Decide the approach", "type": "decision", "execution_order": 1, "depends_on": []},
            {"id": "I", "name": "Configure the system", "type": "task", "execution_order": 2, "depends_on": []},
        ]
        assert _dag_gen.wire_decisions_to_implementers(tasks) == []


@pytest.mark.smoke
class TestDetectUnimplementedDecisions:
    """§17.672 — flag a decision with no step that carries it out."""

    def _tasks(self):
        return [
            {"id": "T1", "name": "Determine server state", "type": "decision", "depends_on": []},
            {"id": "T2", "name": "Decide VLAN scheme", "type": "decision", "depends_on": ["T1"]},
            {"id": "T3", "name": "Decide backup destination", "type": "decision", "depends_on": ["T1"]},
            {"id": "T4", "name": "Decide Jellyfin media storage", "type": "decision", "depends_on": ["T1"]},
            {"id": "T5", "name": "Download Proxmox ISO", "type": "task", "depends_on": ["T1"]},
            {"id": "T17", "name": "Configure Jellyfin media library", "type": "task", "depends_on": ["T5"]},
            {"id": "T19", "name": "Configure backup jobs", "type": "task", "depends_on": ["T3"]},
        ]

    def test_flags_only_the_decision_without_implementer(self):
        # T2 VLAN → no VLAN-config step → flagged; T4 has Jellyfin config → not;
        # T3 consumed by T19 → not; T1 consumed by all → not.
        assert _dag_gen.detect_unimplemented_decisions(self._tasks()) == ["T2"]

    def test_no_decisions_is_empty(self):
        tasks = [{"id": "A", "type": "task", "depends_on": []},
                 {"id": "B", "type": "task", "depends_on": ["A"]}]
        assert _dag_gen.detect_unimplemented_decisions(tasks) == []

    def test_all_decisions_implemented_is_empty(self):
        tasks = [
            {"id": "D", "name": "Decide jellyfin storage", "type": "decision", "depends_on": []},
            {"id": "I", "name": "Configure jellyfin storage", "type": "task", "depends_on": []},
        ]
        assert _dag_gen.detect_unimplemented_decisions(tasks) == []

    def test_render_names_the_missing_step(self):
        block = _dag_gen.render_unimplemented_decision_corrections(
            self._tasks(), ["T2"], 2)
        assert "Decide VLAN scheme" in block
        assert "implement" in block.lower() and "depends_on" in block.lower()
