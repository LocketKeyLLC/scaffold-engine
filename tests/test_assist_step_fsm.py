"""§17.1074 — the assist step state machine: declaration, oracle, and an AST
inventory of every SQL site that writes a step or node status."""
import ast
import pathlib
import re

import pytest

from app.config import settings
from app.modules import assist_step_fsm as fsm

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_machine_builds_and_every_transition_is_legal_by_construction():
    m = fsm._get_machine()
    assert set(m.states) == set(fsm.STEP_STATES)
    for t in fsm.TRANSITIONS:
        srcs = t["source"] if isinstance(t["source"], list) else [t["source"]]
        for s in srcs:
            assert fsm.legal(s, t["dest"]), (s, t["dest"])


@pytest.mark.parametrize("src,dst", [("committed", "presented"), ("skipped", "committed"),
                                     ("pending", "escalated"), ("committed", "skipped")])
def test_illegal_moves_are_refused(src, dst):
    assert not fsm.legal(src, dst)


def test_the_trigger_keeps_the_oracle_sharp():
    assert fsm.legal("pending", "committed", "restore")          # undo a reopen from its pre-image
    assert not fsm.legal("pending", "committed", "commit")       # §17.878 — commit without a claim
    assert fsm.legal("committed", "pending", "reopen") and not fsm.legal("committed", "pending", "unclaim")


def test_mirror_invariant_pairs():
    assert fsm.mirror_ok("committed", "done") and not fsm.mirror_ok("committed", "pending")
    assert fsm.mirror_ok("presented", "pending") and not fsm.mirror_ok("presented", "done")   # the §17.880 shape
    assert fsm.mirror_ok("skipped", "skipped") and not fsm.mirror_ok("pending", "done")       # the §17.911 shape
    assert fsm.mirror_ok(None, "done") and fsm.mirror_ok("committed", None)


def test_check_logs_by_default_and_raises_when_strict(monkeypatch, caplog):
    monkeypatch.setattr(settings, "assist_step_fsm_strict", False, raising=False)
    assert fsm.check("t", src="committed", dst="presented", node_status="pending") is False
    assert any("step_fsm_violation" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(settings, "assist_step_fsm_strict", True, raising=False)
    with pytest.raises(RuntimeError):
        fsm.check("t", src="pending", dst="committed", node_status="done", trigger="commit")
    assert fsm.check("t", src="presented", dst="committed", node_status="done") is True


_STATUS_LIT = re.compile(r"UPDATE\s+(assist_steps|dag_nodes)\b[^;]*?\bstatus\s*=\s*'([a-z_]+)'", re.S)


def _sql_status_writes():
    out = []
    for path in list((ROOT / "app" / "modules").glob("*.py")) + list((ROOT / "app" / "routers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "UPDATE" in node.value:
                for table, status in _STATUS_LIT.findall(node.value):
                    out.append((path.name, node.lineno, table, status))
    return out


def test_every_sql_status_write_targets_a_declared_state():
    writes = _sql_status_writes()
    assert len(writes) >= 20, "the inventory went blind"
    bad = [w for w in writes if (w[2] == "assist_steps" and w[3] not in fsm.STEP_STATES)
           or (w[2] == "dag_nodes" and w[3] not in fsm.NODE_STATES)]
    assert not bad, bad


def test_mermaid_renders_every_transition():
    m = fsm.mermaid()
    assert m.startswith("stateDiagram-v2") and "committed --> pending: reopen" in m and "pending --> presented: claim" in m
