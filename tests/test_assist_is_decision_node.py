"""§17.771 (deferred, now done) — the single decision-node detector.

`is_decision_node` centralizes the bare `node_type.lower()=='decision'` check that
was repeated at ~5 sites (a case-slip/typo at any one silently downgraded a
decision to the committal noncode guide path).
"""
from app.modules import assist_agent as a


def test_is_decision_node_true_cases():
    assert a.is_decision_node("decision")
    assert a.is_decision_node("Decision")
    assert a.is_decision_node("  DECISION  ")  # trimmed + case-insensitive


def test_is_decision_node_false_cases():
    assert not a.is_decision_node("task")
    assert not a.is_decision_node("gather")
    assert not a.is_decision_node(None)
    assert not a.is_decision_node("")


def test_collect_step_kind_routes_through_the_helper():
    assert a._collect_step_kind("decision", "") == "decision"
    assert a._collect_step_kind("Decision", "anything") == "decision"
    assert a._collect_step_kind("task", "just do the thing") is None
