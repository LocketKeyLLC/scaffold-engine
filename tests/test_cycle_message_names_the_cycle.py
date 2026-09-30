"""§17.1210 — a cycle error must name the cycle, and must not blame the edit.

`_validate_graph` answered `"edit would create a dependency cycle"` for a plan
that was already cyclic before anyone touched it, and named no node. Live, on
the operator's 131-node plan carrying EIGHT cycles from earlier re-plans:

    ADD47(skipped) -> ADD48(done) -> ADD47
    ADD17(pending) -> ADD19(done) -> ADD22(done) -> ADD17

Two harms. It blamed the edit and pointed at nothing to look at — and, the bad
one, it made the cycles PERMANENT: every call site validates the whole graph, so
with a cycle present NO edit passed, including the edits that would remove it.
The only tool for fixing a cycle was locked behind the cycle.
"""
from __future__ import annotations

import pytest

from app.modules import node_editor as ne

ACYCLIC = {"T1": [], "T2": ["T1"], "T3": ["T2"]}
CYCLIC = {"T1": ["T3"], "T2": ["T1"], "T3": ["T2"], "T4": []}


# ── it still catches what it always caught ───────────────────────────────

def test_a_clean_graph_passes():
    assert ne._validate_graph(ACYCLIC) is None


def test_unknown_and_self_refs_still_rejected():
    assert "unknown" in (ne._validate_graph({"T1": ["TX"]}) or "")
    assert "itself" in (ne._validate_graph({"T1": ["T1"]}) or "")


def test_an_edit_that_creates_a_cycle_in_a_clean_plan_is_refused():
    edited = {**ACYCLIC, "T1": ["T3"]}
    err = ne._validate_graph(edited, baseline=ACYCLIC)
    assert err and "cycle" in err


# ── it names the cycle ───────────────────────────────────────────────────

def test_the_message_names_the_members():
    err = ne._validate_graph(CYCLIC)
    assert err and "cycle" in err
    for k in ("T1", "T2", "T3"):
        assert k in err, err
    assert "T4" not in err, "a node outside the cycle must not be blamed"


def test_the_cycle_is_shown_as_a_path():
    err = ne._validate_graph(CYCLIC)
    assert "→" in err, err


def test_find_cycle_returns_a_closed_walk():
    cyc = ne._find_cycle(CYCLIC)
    assert cyc and cyc[0] == cyc[-1], cyc
    assert set(cyc) == {"T1", "T2", "T3"}


def test_find_cycle_is_empty_on_a_clean_graph():
    assert ne._find_cycle(ACYCLIC) == []


# ── a broken plan stays repairable ───────────────────────────────────────

def test_an_edit_that_does_not_widen_an_existing_cycle_is_allowed():
    """The whole point. Adding an unrelated leaf to a plan that is already
    cyclic must work, or the plan can never be repaired — which is exactly what
    blocked the recovery step on the operator's job."""
    edited = {**CYCLIC, "NEW": ["T4"]}
    assert ne._validate_graph(edited, baseline=CYCLIC) is None


def test_removing_the_bad_edge_is_allowed_and_then_it_is_clean():
    """Dropping one edge of a cycle is the repair itself."""
    fixed = {**CYCLIC, "T1": []}
    assert ne._validate_graph(fixed, baseline=CYCLIC) is None
    assert ne._validate_graph(fixed) is None


def test_an_edit_that_widens_an_existing_cycle_is_still_refused():
    """"Already broken" is not a licence to make it worse."""
    edited = {**CYCLIC, "T4": ["T5"], "T5": ["T4"]}
    err = ne._validate_graph(edited, baseline=CYCLIC)
    assert err, "a NEW cycle alongside an old one must still be caught"
    assert "T4" in err or "T5" in err, err


def test_the_message_does_not_blame_the_edit_when_the_plan_was_already_broken():
    """Reported behaviour: "edit would create a dependency cycle" on a graph the
    edit had not touched."""
    err = ne._validate_graph(CYCLIC)            # no baseline: describing a plan
    assert "would create" not in err and "this edit" not in err, err
    assert err.startswith("the plan's dependencies contain a cycle"), err


def test_cycle_nodes_counts_what_is_behind_a_cycle_too():
    behind = {**CYCLIC, "T5": ["T1"]}
    assert ne._cycle_nodes(behind) == {"T1", "T2", "T3", "T5"}
    assert "T4" not in ne._cycle_nodes(behind)


# ── every call site passes the baseline ──────────────────────────────────

@pytest.mark.parametrize("fn", ["edit_node", "insert_node", "delete_node"])
def test_each_call_site_judges_against_the_pre_edit_graph(fn):
    import inspect
    src = inspect.getsource(getattr(ne, fn))
    assert "baseline=" in src, f"{fn} validates without a baseline, so it blames its own edit"
