"""Unit tests for validate_dag() and _enforce_node_count().

Run:  pytest tests/test_validate_dag.py -v
"""

import copy
import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so the module can be imported without app dependencies
# ---------------------------------------------------------------------------

import sys
from types import ModuleType
from unittest.mock import MagicMock
from collections import deque

# Stub out app.model_router so import doesn't fail
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

# Now import the functions under test
from importlib import import_module, util as importutil
from pathlib import Path

# Import from the patched file — adjust path as needed
DAG_GEN_PATH = Path(__file__).resolve().parent.parent / "app" / "modules" / "dag_generator.py"
if not DAG_GEN_PATH.exists():
    # Fallback for running from repo root
    DAG_GEN_PATH = Path("app/modules/dag_generator.py")

spec = importutil.spec_from_file_location("dag_generator", DAG_GEN_PATH)
dag_mod = importutil.module_from_spec(spec)
spec.loader.exec_module(dag_mod)

validate_dag = dag_mod.validate_dag
_enforce_node_count = dag_mod._enforce_node_count
VALID_TOOLS = dag_mod.VALID_TOOLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node(nid: str, depends_on: list[str] | None = None, tool: str = "LLM") -> dict:
    """Build a minimal valid node dict."""
    return {
        "id": nid,
        "name": f"Task {nid}",
        "type": "action",
        "inputs": [],
        "outputs": [],
        "depends_on": depends_on or [],
        "tool": tool,
    }


# ═══════════════════════════════════════════════════════════════════════════
# validate_dag tests
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateDAG:

    # 1. Valid DAG — no warnings, no errors
    def test_valid_dag(self):
        nodes = [_node("T1"), _node("T2", ["T1"]), _node("T3", ["T2"])]
        cleaned, warnings = validate_dag(copy.deepcopy(nodes))
        assert len(cleaned) == 3
        assert warnings == []
        assert cleaned[1]["depends_on"] == ["T1"]

    # 2. Invalid dependency reference — stripped, warning logged
    def test_invalid_dependency_reference(self):
        nodes = [_node("T1"), _node("T2", ["T1", "T0"])]
        cleaned, warnings = validate_dag(copy.deepcopy(nodes))
        assert cleaned[1]["depends_on"] == ["T1"]
        assert any("invalid_dependency" in w and "T0" in w for w in warnings)

    # 3. Self-reference — stripped, warning logged
    def test_self_reference(self):
        nodes = [_node("T1"), _node("T2", ["T1", "T2"])]
        cleaned, warnings = validate_dag(copy.deepcopy(nodes))
        assert "T2" not in cleaned[1]["depends_on"]
        assert any("self_reference_removed" in w for w in warnings)

    # 4. Cycle detection — raises ValueError
    def test_cycle_detected(self):
        nodes = [
            _node("T1", ["T3"]),
            _node("T2", ["T1"]),
            _node("T3", ["T2"]),
        ]
        with pytest.raises(ValueError, match="dag_cycle_detected"):
            validate_dag(copy.deepcopy(nodes))

    # 5. Invalid tool — defaults to LLM, warning logged
    def test_invalid_tool_defaulted(self):
        nodes = [_node("T1", tool="Photoshop"), _node("T2", ["T1"])]
        cleaned, warnings = validate_dag(copy.deepcopy(nodes))
        assert cleaned[0]["tool"] == "LLM"
        assert any("invalid_tool_defaulted" in w and "Photoshop" in w for w in warnings)

    # 6. All valid tools accepted without warnings
    def test_all_valid_tools_accepted(self):
        nodes = [_node(f"T{i+1}", tool=t) for i, t in enumerate(sorted(VALID_TOOLS))]
        # Make a simple chain
        for i in range(1, len(nodes)):
            nodes[i]["depends_on"] = [nodes[i - 1]["id"]]
        cleaned, warnings = validate_dag(copy.deepcopy(nodes))
        tool_warnings = [w for w in warnings if "invalid_tool" in w]
        assert tool_warnings == []

    # 7. Empty depends_on — valid, no warnings
    def test_empty_depends_on(self):
        nodes = [_node("T1"), _node("T2")]
        cleaned, warnings = validate_dag(copy.deepcopy(nodes))
        assert len(cleaned) == 2
        # No dep-related warnings
        dep_warnings = [w for w in warnings if "dependency" in w or "self_reference" in w]
        assert dep_warnings == []

    # 8. Disconnected subgraph — still valid (no cycle)
    def test_disconnected_subgraph(self):
        nodes = [
            _node("T1"),
            _node("T2", ["T1"]),
            _node("T3"),  # disconnected
            _node("T4", ["T3"]),  # disconnected chain
        ]
        cleaned, warnings = validate_dag(copy.deepcopy(nodes))
        assert len(cleaned) == 4
        # No cycle error, no dep errors

    # 9. Multiple invalid refs on single node
    def test_multiple_invalid_refs(self):
        nodes = [_node("T1"), _node("T2", ["T0", "T9", "T1"])]
        cleaned, warnings = validate_dag(copy.deepcopy(nodes))
        assert cleaned[1]["depends_on"] == ["T1"]
        invalid_warnings = [w for w in warnings if "invalid_dependency" in w]
        assert len(invalid_warnings) == 2

    # 10. Self-ref + invalid ref combo
    def test_self_ref_and_invalid_ref(self):
        nodes = [_node("T1"), _node("T2", ["T2", "T99"])]
        cleaned, warnings = validate_dag(copy.deepcopy(nodes))
        assert cleaned[1]["depends_on"] == []
        assert any("self_reference" in w for w in warnings)
        assert any("invalid_dependency" in w for w in warnings)


# ═══════════════════════════════════════════════════════════════════════════
# _enforce_node_count tests
# ═══════════════════════════════════════════════════════════════════════════

class TestEnforceNodeCount:

    # 11. >5 nodes truncated to 5 by key sort
    def test_truncate_over_5(self):
        nodes = [_node(f"T{i}") for i in range(1, 13)]
        # T4 depends on T11 (which will be dropped)
        nodes[3]["depends_on"] = ["T11"]
        result = _enforce_node_count(copy.deepcopy(nodes))
        assert len(result) == 10
        ids = {n["id"] for n in result}
        assert "T11" not in ids
        assert "T12" not in ids
        # Dangling ref to T6 must be cleaned
        t4 = next(n for n in result if n["id"] == "T4")
        assert "T11" not in t4["depends_on"]

    # 12. <3 nodes — accepted, no truncation
    def test_undercount_accepted(self):
        nodes = [_node("T1"), _node("T2", ["T1"])]
        result = _enforce_node_count(copy.deepcopy(nodes))
        assert len(result) == 2

    # 13. Exactly 5 nodes — no change
    def test_exactly_5(self):
        nodes = [_node(f"T{i}") for i in range(1, 6)]
        result = _enforce_node_count(copy.deepcopy(nodes))
        assert len(result) == 5

    # 14. Exactly 3 nodes — no change
    def test_exactly_3(self):
        nodes = [_node(f"T{i}") for i in range(1, 4)]
        result = _enforce_node_count(copy.deepcopy(nodes))
        assert len(result) == 3
