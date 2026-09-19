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


# ── §17.1112 (Phase 1 ledger D-3) — oracle coverage ratchet ─────────────────
#
# The FSM was declared in §17.1074 as the oracle, but only 3 of the write sites
# called it; goto, reopen, retire, submit and handoff wrote unguarded. This
# counts every `UPDATE assist_steps … SET status='…'` site under app/modules +
# app/routers that has NO `_fsm_check(` / `assist_step_fsm.check(` within the
# 40 lines above it. The number can only go DOWN: a new status write must run
# the oracle, and migrating an old one lowers the ceiling.
import re as _re

_STEP_STATUS_WRITE = _re.compile(r"UPDATE\s+assist_steps\b(?:(?!\bWHERE\b)[\s\S]){0,400}?\bstatus\s*=\s*'")
_UNCHECKED_CEILING = 10   # 2026-09-19 §17.1112: 19 sites, 10 unchecked (was 16 unchecked before goto/reopen/retire)


def _unchecked_step_status_writes() -> list[tuple[str, int]]:
    out = []
    for path in sorted(list((ROOT / "app" / "modules").glob("*.py")) + list((ROOT / "app" / "routers").glob("*.py"))):
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for m in _STEP_STATUS_WRITE.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            above = "\n".join(lines[max(0, line - 41):line - 1])
            if "_fsm_check(" not in above and "assist_step_fsm.check(" not in above:
                out.append((path.name, line))
    return out


def test_oracle_ratchet_regex_hits_the_write_shapes():
    assert _STEP_STATUS_WRITE.search("UPDATE assist_steps SET status='committed', committed_at=NOW() WHERE x")
    assert _STEP_STATUS_WRITE.search('text("UPDATE assist_steps "\n "   SET status = \'presented\', "\n " WHERE session_id = :sid")')
    assert not _STEP_STATUS_WRITE.search("UPDATE assist_steps SET guidance = :g WHERE session_id = :sid AND status = 'presented'")


def test_goto_reopen_and_retire_run_the_oracle():
    """The three §17.1112 sites are checked — by name, so the ratchet cannot be
    satisfied by moving code around."""
    unchecked = {(f, l) for f, l in _unchecked_step_status_writes()}
    agent = (ROOT / "app" / "modules" / "assist_agent.py").read_text(encoding="utf-8")
    router = (ROOT / "app" / "routers" / "assist.py").read_text(encoding="utf-8")
    for src, name, marker in ((agent, "assist_agent.py", "keep_node_key=node_key, site=\"goto_step\""),
                              (agent, "assist_agent.py", "site=\"reopen_step\""),
                              (router, "assist.py", "\"tracker_retire\", src=_prior")):
        assert marker in src, f"{name}: {marker} missing"
    assert not any(f == "assist.py" and 1300 < l < 1420 for f, l in unchecked), "retire writes are unchecked"


def test_unchecked_step_status_writes_only_go_down():
    unchecked = _unchecked_step_status_writes()
    assert len(unchecked) <= _UNCHECKED_CEILING, (
        f"{len(unchecked)} assist_steps status writes without an FSM check (ceiling {_UNCHECKED_CEILING}); "
        f"run assist_step_fsm.check(...) before the write, or lower the ceiling if you migrated sites: {unchecked}")
    assert len(unchecked) == _UNCHECKED_CEILING, f"ceiling is loose: set it to {len(unchecked)}"

