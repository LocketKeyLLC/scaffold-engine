"""§17.696 — a cyclic generated DAG is REPAIRED (back-edges removed), not failed.

Reported: the "VLAN Segmentation & AdGuard Home DNS" component job failed with 0
nodes — error_summary `dag_cycle_detected: involved_keys=[T1..T19]`. The LLM
produced a dependency cycle; validate_dag raised and the whole component failed.
Now break_cycles deterministically removes the minimal back-edges so the graph
becomes acyclic and the job proceeds, like the §17.668-670 well-formedness passes.
"""
from __future__ import annotations

import pytest

from app.config import settings as _settings
from app.modules.dag_generator import (
    break_cycles,
    validate_dag,
    _acyclic_residual,
)


def _n(k, deps, tool="LLM"):
    return {"id": k, "name": k, "depends_on": list(deps), "tool": tool}


def test_break_cycles_removes_minimal_back_edge():
    # T1→T2→T3→T1 cycle (+ T4 tail). One back-edge removed ⇒ acyclic chain.
    nodes = [_n("T1", ["T3"]), _n("T2", ["T1"]), _n("T3", ["T2"]), _n("T4", ["T3"])]
    assert _acyclic_residual(nodes)            # cyclic to start
    removed = break_cycles(nodes)
    assert len(removed) == 1                   # minimal
    assert _acyclic_residual(nodes) == []      # now acyclic
    # the forward chain is preserved
    deps = {x["id"]: x["depends_on"] for x in nodes}
    assert deps["T2"] == ["T1"] and deps["T3"] == ["T2"] and deps["T4"] == ["T3"]


def test_break_cycles_noop_on_acyclic():
    nodes = [_n("T1", []), _n("T2", ["T1"]), _n("T3", ["T1", "T2"])]
    assert break_cycles(nodes) == []
    assert _acyclic_residual(nodes) == []


def test_break_cycles_handles_larger_multi_cycle():
    # Two interlocking cycles; result must be fully acyclic.
    nodes = [
        _n("T1", ["T5"]), _n("T2", ["T1"]), _n("T3", ["T2"]),
        _n("T4", ["T3"]), _n("T5", ["T4", "T2"]),
    ]
    assert _acyclic_residual(nodes)
    break_cycles(nodes)
    assert _acyclic_residual(nodes) == []


def test_validate_dag_repairs_cycle_instead_of_raising(monkeypatch):
    monkeypatch.setattr(_settings, "dag_break_cycles_enabled", True)
    nodes = [_n("T1", ["T3"]), _n("T2", ["T1"]), _n("T3", ["T2"])]
    cleaned, warnings = validate_dag(nodes)     # must NOT raise
    assert _acyclic_residual(cleaned) == []
    assert any("dag_cycle_broken" in w for w in warnings)


def test_validate_dag_gate_off_still_raises(monkeypatch):
    monkeypatch.setattr(_settings, "dag_break_cycles_enabled", False)
    nodes = [_n("T1", ["T2"]), _n("T2", ["T1"])]
    with pytest.raises(ValueError, match="dag_cycle_detected"):
        validate_dag(nodes)
