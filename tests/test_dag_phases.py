"""§17.1007 — phase chunking for the assist walkthrough (app/modules/dag_phases.py).

These tests exist because the first two implementations both LOOKED right and
both produced a phase holding a quarter to a half of the plan — which is the
flat progress bar the module was written to remove, wearing a phase label. The
balance assertions below are the actual contract; the shapes they run over are
the shapes this engine really plans (near-linear chains, plus the one real DAG
on the box at the time of writing).
"""
from __future__ import annotations

from collections import defaultdict

import pytest

from app.modules.dag_phases import (
    MIN_NODES_FOR_PHASES,
    TARGET_PHASES,
    compute_phases,
)


def linear(n: int) -> list[dict]:
    """T1 → T2 → … → Tn, the shape most plans here actually have."""
    return [
        {
            "node_key": f"T{i}",
            "depends_on": ([f"T{i - 1}"] if i > 1 else []),
            "execution_order": i,
        }
        for i in range(1, n + 1)
    ]


def phase_sizes(result: dict) -> list[int]:
    counts: dict[int, int] = defaultdict(int)
    for info in result.values():
        counts[info["phase"]] += 1
    return [counts[p] for p in sorted(counts)]


def assert_well_formed(nodes: list[dict], result: dict) -> None:
    """Invariants every non-empty result must satisfy."""
    assert len(result) == len(nodes), "every node must land in exactly one phase"
    totals = {i["phase_total"] for i in result.values()}
    assert len(totals) == 1, f"phase_total must agree across nodes, got {totals}"

    by_phase: dict[int, list[dict]] = defaultdict(list)
    for info in result.values():
        by_phase[info["phase"]].append(info)
    assert sorted(by_phase) == list(range(1, totals.pop() + 1)), "phases are 1..N, dense"

    for phase, infos in by_phase.items():
        positions = sorted(i["phase_pos"] for i in infos)
        assert positions == list(range(1, len(infos) + 1)), (
            f"phase {phase} positions must be 1..{len(infos)}, got {positions}"
        )
        sizes = {i["phase_size"] for i in infos}
        assert sizes == {len(infos)}, f"phase {phase} disagrees about its own size: {sizes}"


# ── Too small / too flat to chunk ────────────────────────────────────────

@pytest.mark.parametrize("n", [0, 1, 5, MIN_NODES_FOR_PHASES - 1])
def test_short_plans_are_not_chunked(n):
    """A plan you can hold in your head needs no phases; the client falls back
    to the plain total. Empty is a signal, not a failure."""
    assert compute_phases(linear(n)) == {}


def test_single_level_is_not_chunked():
    """20 independent nodes have no sequence to chunk — inventing boundaries
    would imply an ordering the plan does not have."""
    nodes = [{"node_key": f"T{i}", "depends_on": [], "execution_order": i} for i in range(1, 21)]
    assert compute_phases(nodes) == {}


# ── The balance contract (the bug both earlier versions had) ─────────────

@pytest.mark.parametrize("n", [12, 20, 41, 60, 100])
def test_linear_plans_are_evenly_chunked(n):
    result = compute_phases(linear(n))
    assert_well_formed(linear(n), result)
    sizes = phase_sizes(result)
    assert len(sizes) == TARGET_PHASES
    # No phase may exceed the mean by more than one node. A greedy fill passed
    # every other assertion here while producing [6,6,6,6,6,11] on n=41.
    assert max(sizes) - min(sizes) <= 1, f"unbalanced: {sizes}"
    assert sum(sizes) == n


def test_no_phase_swallows_the_plan():
    """The regression that motivated the module: one phase holding ~44% of a
    41-step plan reads exactly like the flat bar it replaced."""
    result = compute_phases(linear(41))
    assert max(phase_sizes(result)) <= 0.30 * 41


# ── Dependency structure is respected ────────────────────────────────────

def test_phase_boundaries_never_run_backwards():
    """A node may never sit in an earlier phase than something it depends on —
    a phase boundary has to mean 'this cannot start until that is finished'."""
    nodes = linear(30)
    # add a parallel branch off T5 that rejoins at T20
    nodes += [
        {"node_key": "B1", "depends_on": ["T5"], "execution_order": 6},
        {"node_key": "B2", "depends_on": ["B1"], "execution_order": 7},
    ]
    nodes = [
        dict(n, depends_on=(n["depends_on"] + ["B2"]) if n["node_key"] == "T20" else n["depends_on"])
        for n in nodes
    ]
    result = compute_phases(nodes)
    assert_well_formed(nodes, result)
    for node in nodes:
        for dep in node["depends_on"]:
            assert result[dep]["phase"] <= result[node["node_key"]]["phase"], (
                f"{node['node_key']} is scheduled before its dependency {dep}"
            )


def test_parallel_siblings_share_a_phase():
    """Two steps that can run at the same time must not be split by a boundary
    that claims one blocks the other."""
    nodes = linear(12)
    nodes += [
        {"node_key": f"P{i}", "depends_on": ["T3"], "execution_order": 4} for i in range(1, 5)
    ]
    result = compute_phases(nodes)
    assert_well_formed(nodes, result)
    assert len({result[f"P{i}"]["phase"] for i in range(1, 5)}) == 1


# ── Degrades rather than raising ─────────────────────────────────────────

def test_cycle_does_not_raise():
    """The well-formedness passes (§17.668–670) should prevent cycles, but a
    progress badge must never be the thing that breaks the walkthrough."""
    nodes = [
        {"node_key": "A", "depends_on": ["C"], "execution_order": 1},
        {"node_key": "B", "depends_on": ["A"], "execution_order": 2},
        {"node_key": "C", "depends_on": ["B"], "execution_order": 3},
    ] + [{"node_key": f"D{i}", "depends_on": [], "execution_order": 10 + i} for i in range(9)]
    assert compute_phases(nodes) == {}  # collapses to one level → not chunked


def test_dangling_dependencies_are_ignored():
    """A dep naming a node that isn't in the set (a pruned or renamed step)
    must be treated as absent, not crash the walk."""
    nodes = [
        {"node_key": f"T{i}", "depends_on": ["GHOST"], "execution_order": i}
        for i in range(1, 15)
    ]
    assert compute_phases(nodes) == {}  # all roots → one level


def test_missing_execution_order_still_orders_within_a_phase():
    """execution_order is nullable on dag_nodes; ordering must fall back to the
    node key rather than raising on a None comparison."""
    nodes = [
        {"node_key": f"T{i}", "depends_on": ([f"T{i - 1}"] if i > 1 else []), "execution_order": None}
        for i in range(1, 15)
    ]
    result = compute_phases(nodes)
    assert_well_formed(nodes, result)


def test_nodes_without_a_key_are_dropped():
    nodes = linear(14) + [{"node_key": "", "depends_on": [], "execution_order": 99}]
    result = compute_phases(nodes)
    assert "" not in result
    assert len(result) == 14


def uneven_widths() -> list[dict]:
    """Two narrow levels, then a 12-wide fan-out, then a narrow tail.

    This shape is the regression test for the midpoint placement. Keyed on the
    work PRECEDING a level, a wide level is placed in the bucket its
    predecessors already occupy and drags them in with it — on the real 41-node
    plan that produced a 15-step opening phase while every other assertion in
    this file stayed green.
    """
    nodes = [
        {"node_key": "A0", "depends_on": [], "execution_order": 1},
        {"node_key": "A1", "depends_on": ["A0"], "execution_order": 2},
    ]
    nodes += [
        {"node_key": f"W{i}", "depends_on": ["A1"], "execution_order": 2 + i}
        for i in range(1, 13)
    ]
    nodes.append({"node_key": "B0", "depends_on": [f"W{i}" for i in range(1, 13)], "execution_order": 20})
    for i in range(1, 5):
        nodes.append({"node_key": f"B{i}", "depends_on": [f"B{i - 1}"], "execution_order": 20 + i})
    return nodes


def test_a_wide_level_does_not_absorb_the_levels_before_it():
    """A level too wide to share a bucket must stand alone, not annex its
    narrow predecessors. Placing levels by preceding work instead of by
    midpoint puts A0/A1 in the same phase as the 12-wide fan-out."""
    nodes = uneven_widths()
    result = compute_phases(nodes)
    assert_well_formed(nodes, result)
    wide_phase = result["W1"]["phase"]
    in_wide_phase = {k for k, v in result.items() if v["phase"] == wide_phase}
    assert in_wide_phase == {f"W{i}" for i in range(1, 13)}, (
        "the wide level should be its own phase, got " + repr(sorted(in_wide_phase))
    )


def test_uneven_widths_keep_the_head_phase_small():
    """The head phase must reflect the work actually at the front of the plan
    (2 steps here), not everything that happens to precede a wide level."""
    result = compute_phases(uneven_widths())
    assert phase_sizes(result)[0] <= 3
